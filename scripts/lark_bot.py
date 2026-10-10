#!/usr/bin/env python3
"""Lark bot that runs AI-speech detection on audio files sent to it.

  Direct chat: send the bot an audio file.
  Group chat:  reply to the audio file's message and @ the bot.

Connects out to Lark over a long connection, so no public URL is needed.
Needs LARK_APP_ID and LARK_APP_SECRET in the environment / .env, plus at least
one detector key (see detect_ai.py). Set LARK_DOMAIN=feishu for Feishu tenants.

  lark_bot.py --setup   create the Lark app by approving a link, and save its
                        credentials to .env
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import detect_ai

detect_ai.load_env()
# The SDK would otherwise pick up an ambient proxy; connect directly like detect_ai does.
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"

import lark_oapi as lark
import requests
from lark_oapi.api.im.v1 import (
    GetMessageRequest,
    GetMessageResourceRequest,
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

log = logging.getLogger("lark_bot")

AUDIO_MESSAGE_TYPES = {"file", "audio", "media"}
VERDICT_TEXT = {
    "FLAG": "AI",
    "REVIEW": "Needs review",
    "PASS": "Real",
    "PASS (single detector)": "Real",
}
USAGE = "Send me one audio file directly. In a group, reply to that file's message and @ me. One file per request."
DOWNLOAD_ATTEMPTS = 3
DETECT_ARGS = argparse.Namespace(zero_retention=False, timeout=180)

client: lark.Client
active: list[str]
workers = ThreadPoolExecutor(max_workers=2)
seen: set[str] = set()  # Lark re-delivers events it thinks were missed


def reply(message_id: str, text: str) -> None:
    request = (
        ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            ReplyMessageRequestBody.builder().msg_type("text").content(json.dumps({"text": text})).build()
        )
        .build()
    )
    response = client.im.v1.message.reply(request)
    if not response.success():
        log.error("reply failed: %s %s", response.code, response.msg)


class UserError(Exception):
    """A failure whose message says what went wrong and what to do, safe to show in the chat."""


def find_audio(message) -> tuple[str, str, dict] | None:
    """Return (message_id, message_type, content) of the audio this message carries or replies to."""
    if message.message_type in AUDIO_MESSAGE_TYPES:
        return message.message_id, message.message_type, json.loads(message.content)
    if not message.parent_id:
        return None
    response = client.im.v1.message.get(GetMessageRequest.builder().message_id(message.parent_id).build())
    if not response.success():
        raise UserError(
            f"I could not read the message you replied to ({response.code}: {response.msg}). "
            "Send the audio file to me directly instead."
        )
    parent = response.data.items[0]
    if parent.msg_type not in AUDIO_MESSAGE_TYPES:
        raise UserError(
            f"The message you replied to is not an audio file (it is of type '{parent.msg_type}'). "
            "Reply to the audio file's own message and @ me."
        )
    return parent.message_id, parent.msg_type, json.loads(parent.body.content)


def download(message_id: str, file_key: str, name: str, folder: Path) -> Path:
    request = GetMessageResourceRequest.builder().message_id(message_id).file_key(file_key).type("file").build()
    problem = ""
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            response = client.im.v1.message_resource.get(request)
        except requests.RequestException as e:
            problem = f"network error, {type(e).__name__}"
        else:
            if response.success():
                path = folder / name
                path.write_bytes(response.file.read())
                return path
            if response.code is not None:
                # Lark answered with its own error; asking again will not change it.
                raise UserError(
                    f"Lark refused the download ({response.code}: {response.msg}). "
                    "Download the recording and resend it to me as a file."
                )
            # Lark's gateway times out now and then (HTTP 504 with an empty body) when
            # several downloads run at once; the same request succeeds moments later.
            problem = f"HTTP {response.raw.status_code} from Lark"
        log.warning("download of %s failed, attempt %d/%d: %s", name, attempt, DOWNLOAD_ATTEMPTS, problem)
        if attempt < DOWNLOAD_ATTEMPTS:
            time.sleep(2 * attempt)
    raise UserError(
        f"Lark did not return the file after {DOWNLOAD_ATTEMPTS} tries ({problem}). Please @ me on it again in a minute."
    )


def format_report(report: dict, name: str) -> str:
    lines = [f"File: {name}", f"Voice AI or Real: {VERDICT_TEXT[report['verdict']]}"]
    several = len(report["providers"]) > 1
    for provider, r in report["providers"].items():
        if several:
            lines.append(f"[{provider}] {r['label']}")
        if r["label"] == "unknown":
            lines.append(f"Detail: {r['detail']}")
        lines.extend(f"{key}: {value}" for key, value in r["fields"].items() if value is not None)
    return "\n".join(lines)


def display_name(message_type: str, content: dict, name: str) -> str:
    """What to call the recording in replies.

    Lark keeps the original name only for file attachments. A video or voice
    message posted straight into the chat is exposed under an internal ID, so
    those are described by kind and length instead.
    """
    kind = {"media": "Video posted in chat", "audio": "Voice message"}.get(message_type)
    if kind is None:
        return name
    seconds = int(content.get("duration") or 0) // 1000
    return f"{kind} ({seconds // 60}:{seconds % 60:02d})"


def handle(message) -> None:
    target, prefix = message.message_id, ""
    try:
        audio = find_audio(message)
        if audio is None:
            reply(target, USAGE)
            return
        source_id, message_type, content = audio
        # Voice messages carry no file name; Lark records them as Opus.
        name = Path(content.get("file_name") or f"voice-message.{'opus' if message_type == 'audio' else 'bin'}").name
        # Answer on the file's own message and lead with its name, so each
        # result can be matched to its file when several are checked at once.
        label = display_name(message_type, content, name)
        target, prefix = source_id, f"File: {label}\n"
        with tempfile.TemporaryDirectory() as folder:
            path = download(source_id, content["file_key"], name, Path(folder))
            reply(target, f"{prefix}Checking. This usually takes under a minute.")
            report = detect_ai.detect(path, active, DETECT_ARGS, escalate=True)
        for provider, r in report["providers"].items():
            if r["label"] == "unknown":
                log.warning("%s: %s gave no verdict: %s", name, provider, r["detail"])
        log.info("%s -> %s", name, report["verdict"])
        reply(target, format_report(report, label))
    except UserError as e:
        log.warning("message %s: %s", message.message_id, e)
        reply(target, f"{prefix}Check failed: {e}")
    except Exception as e:
        log.exception("handling message %s failed", message.message_id)
        reply(
            target,
            f"{prefix}Check failed: unexpected error ({type(e).__name__}: {e}). "
            "Please try again; if it keeps failing, tell the bot's maintainer.",
        )


def on_message(data: P2ImMessageReceiveV1) -> None:
    message = data.event.message
    if message.message_id in seen:
        return
    seen.add(message.message_id)
    # In groups only act when @-mentioned, so the bot stays quiet even if it is
    # later granted access to every group message.
    if message.chat_type == "group" and not message.mentions:
        return
    # Detection outlasts Lark's 3-second acknowledgement window, so hand it off.
    workers.submit(handle, message)


def save_env(values: dict[str, str]) -> Path:
    env_file = Path(__file__).resolve().parent.parent / ".env"
    lines = env_file.read_text().splitlines() if env_file.exists() else []
    lines = [line for line in lines if line.split("=", 1)[0].strip() not in values]
    env_file.write_text("\n".join(lines + [f"{k}={v}" for k, v in values.items()]) + "\n")
    return env_file


def setup() -> int:
    """Create the app through Lark's one-click registration; the user approves it in Lark."""
    feishu = os.environ.get("LARK_DOMAIN", "lark").lower() == "feishu"
    accounts = lark.FEISHU_OAUTH_DOMAIN if feishu else lark.LARK_OAUTH_DOMAIN

    def show_link(info: dict) -> None:
        print(f"Open this link in Lark and approve it (valid {info['expire_in'] // 60} min):\n{info['url']}", flush=True)

    result = lark.register_app(
        on_qr_code=show_link,
        domain=accounts,
        lark_domain=lark.LARK_OAUTH_DOMAIN,
        app_preset={"name": "Voice Verifier", "desc": "Checks whether a voice recording is AI-generated"},
    )
    env_file = save_env({"LARK_APP_ID": result["client_id"], "LARK_APP_SECRET": result["client_secret"]})
    print(f"App {result['client_id']} created; credentials saved to {env_file}")
    return 0


def main() -> int:
    global client, active
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--setup", action="store_true", help="create the Lark app and save its credentials to .env")
    if parser.parse_args().setup:
        return setup()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app_id, app_secret = os.environ.get("LARK_APP_ID"), os.environ.get("LARK_APP_SECRET")
    if not app_id or not app_secret:
        print("Set LARK_APP_ID and LARK_APP_SECRET in .env", file=sys.stderr)
        return 2
    active = detect_ai.configured()
    if not active:
        print("No detector is configured; see .env.example", file=sys.stderr)
        return 2

    domain = lark.FEISHU_DOMAIN if os.environ.get("LARK_DOMAIN", "lark").lower() == "feishu" else lark.LARK_DOMAIN
    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).domain(domain).build()
    handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(on_message).build()
    log.info("detectors: %s; connecting to %s", ", ".join(active), domain)
    lark.ws.Client(app_id, app_secret, event_handler=handler, domain=domain, log_level=lark.LogLevel.INFO).start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
