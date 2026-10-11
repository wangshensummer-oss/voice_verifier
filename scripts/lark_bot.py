#!/usr/bin/env python3
"""Lark bot that runs AI-speech detection on audio files sent to it.

  Single check, direct chat: send the bot an audio file.
  Single check, group chat:  reply to the audio file's message and @ the bot.
  Single check by link: send the bot a Google Drive link to the recording.
  Batch check: send the bot (in a group, @ it with) a link to a Lark Sheet that
               has an "Attachment" column of audio attachments; the same result
               fields are written into the sheet. See lark_sheet.py.

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
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import detect_ai

detect_ai.load_env()
# The SDK would otherwise pick up an ambient proxy; connect directly like detect_ai does.
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"

import lark_oapi as lark
import requests
from lark_oapi.api.drive.v1 import DownloadMediaRequest
from lark_oapi.api.im.v1 import (
    GetMessageRequest,
    GetMessageResourceRequest,
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

import lark_sheet
import link_fetch
from lark_sheet import UserError

log = logging.getLogger("lark_bot")

AUDIO_MESSAGE_TYPES = {"file", "audio", "media"}
VERDICT_TEXT = {
    "FLAG": "AI",
    "REVIEW": "Needs review",
    "PASS": "Real",
    "PASS (single detector)": "Real",
}
USAGE = (
    "To check one recording, send me the audio file or a Google Drive link to it; "
    "in a group, reply to the file's message and @ me.\n"
    "To check many, send me a link to a Lark Sheet with an 'Attachment' column of audio attachments; "
    "I write the same result into the sheet, one row per recording."
)
UNEXPECTED = "Something went wrong on my side. Please try again; if it keeps failing, tell the bot's maintainer."
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_CHUNK = 8 * 1024 * 1024
# Rows checked per batch request; sending the link again continues with the rest.
BATCH_ROWS = 200
DETECT_ARGS = argparse.Namespace(zero_retention=False, timeout=180)

client: lark.Client
active: list[str]
workers = ThreadPoolExecutor(max_workers=4)
seen: set[str] = set()  # Lark re-delivers events it thinks were missed
batches_running: set[str] = set()  # spreadsheet tokens, so one sheet is not checked twice at once
batches_lock = threading.Lock()


def reply(message_id: str, text: str) -> None:
    request = (
        ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            ReplyMessageRequestBody.builder()
            .msg_type("text")
            .content(json.dumps({"text": text}))
            .uuid(uuid.uuid4().hex)  # lets Lark drop the duplicate if a retry repeats a delivered reply
            .build()
        )
        .build()
    )
    try:
        response = lark_sheet.with_retry(lambda: client.im.v1.message.reply(request))
    except UserError:
        log.error("reply to %s could not be sent: %s", message_id, text)
        return
    if not response.success():
        log.error("reply failed: %s %s", response.code, response.msg)


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


def fetch(name: str, send, if_refused: str):
    """Run one Lark download request, retrying transient failures, and return its response."""
    problem = ""
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            response = send()
        except requests.RequestException as e:
            problem = "the connection to Lark dropped"
            log.warning("download of %s: %r", name, e)
        else:
            if response.success():
                return response
            if response.code is not None:
                # Lark answered with its own error; asking again will not change it.
                raise UserError(f"Lark refused the download ({response.code}: {response.msg}). {if_refused}")
            # Lark's gateway times out now and then (HTTP 504 with an empty body) when
            # several downloads run at once; the same request succeeds moments later.
            status = response.raw.status_code
            problem = "Lark timed out" if status == 504 else f"Lark returned error {status}"
        log.warning("download of %s failed, attempt %d/%d: %s", name, attempt, DOWNLOAD_ATTEMPTS, problem)
        if attempt < DOWNLOAD_ATTEMPTS:
            time.sleep(2 * attempt)
    raise UserError(f"Lark did not return the file after {DOWNLOAD_ATTEMPTS} tries ({problem}). Please try again in a minute.")


def download(message_id: str, file_key: str, name: str, folder: Path) -> Path:
    request = GetMessageResourceRequest.builder().message_id(message_id).file_key(file_key).type("file").build()
    response = fetch(
        name,
        lambda: client.im.v1.message_resource.get(request),
        "Download the recording and resend it to me as a file.",
    )
    path = folder / name
    path.write_bytes(response.file.read())
    return path


def download_attachment(file_token: str, name: str, folder: Path) -> Path:
    """Download a sheet attachment in ranges; Lark drops single responses larger than 16 MB."""
    request = DownloadMediaRequest.builder().file_token(file_token).build()
    path, start = folder / name, 0
    with path.open("wb") as out:
        while True:
            option = lark.RequestOption.builder().headers({"Range": f"bytes={start}-{start + DOWNLOAD_CHUNK - 1}"}).build()
            response = fetch(name, lambda: client.drive.v1.media.download(request, option), lark_sheet.SHARE_HINT)
            data = response.file.read()
            out.write(data)
            start += len(data)
            headers = {key.lower(): value for key, value in response.raw.headers.items()}
            total = headers.get("content-range", "").rpartition("/")[2]
            if not data or not total.isdigit() or start >= int(total):
                return path


def percent(score: float) -> str:
    if score < 0.01:
        return "under 1%"
    if score > 0.99:
        return "over 99%"
    return f"{round(score * 100)}%"


CHECK_LABEL = {"real": "Real", "fake": "AI", "partial": "Partly AI"}
ORDINALS = ("first check", "second check", "third check")
# Why a check gave no result, by what its error text contains. The text itself
# (hosts, endpoints, exception names) stays in the log.
NO_RESULT_REASONS = (
    ("insufficient balance", "out of credit"),
    ("unsupported file type", "file type not supported"),
    ("could not analyse", "could not read the audio"),
    ("no speech", "no speech found"),
    ("still processing", "timed out"),
    ("timed out", "timed out"),
    ("timeout", "timed out"),
)


def likelihood(report: dict) -> str:
    """The AI likelihood of each check that reached a verdict, as percentages."""
    scores = [r["score"] for r in report["providers"].values() if r["label"] != "unknown" and r["score"] is not None]
    if len(scores) == 1:
        return percent(scores[0])
    return ", ".join(f"{percent(score)} ({ORDINALS[i]})" for i, score in enumerate(scores))


def details(report: dict) -> list[str]:
    """Why a review is needed, then what each check found, in the same terms whichever detector ran it."""
    results = list(report["providers"].values())
    lines = []
    if report["verdict"] == "REVIEW":
        labels = {r["label"] for r in results}
        if {"fake", "real"} <= labels:
            lines.append("The checks disagree, so please listen to it yourself.")
        elif "partial" in labels:
            lines.append("Part of the recording looks AI-generated, so please listen to it yourself.")
        else:
            lines.append("The check could not be completed. Please try again in a few minutes.")
    for number, r in enumerate(results, start=1):
        if r["label"] == "unknown":
            text = r["detail"].lower()
            reason = next((reason for needle, reason in NO_RESULT_REASONS if needle in text), "service error")
            lines.append(f"Check {number}: no result ({reason})")
            continue
        parts = [CHECK_LABEL[r["label"]]]
        if r["score"] is not None:
            parts.append(f"AI likelihood {percent(r['score'])}")
        if r["fields"].get("Peak_frame") is not None:
            parts.append(f"most suspicious segment {percent(float(r['fields']['Peak_frame']))}")
        if r["fields"].get("Consistency") is not None:
            parts.append(f"consistency across segments {round(float(r['fields']['Consistency']))}%")
        lines.append(f"Check {number}: " + ", ".join(parts))
    return lines


def format_report(report: dict, name: str) -> str:
    lines = [
        f"File: {name}",
        f"Voice AI or Real: {VERDICT_TEXT[report['verdict']]}",
        f"AI likelihood: {likelihood(report) or 'not available'}",
        "Details:",
    ]
    return "\n".join(lines + details(report))


def log_report(name: str, report: dict) -> None:
    log.info("%s -> %s %s", name, report["verdict"], json.dumps(report["providers"], ensure_ascii=False))


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


def check_row(row: lark_sheet.Row) -> list[str]:
    """Run detection for one sheet row and return its lark_sheet.OUTPUTS cells."""
    drive_id = link_fetch.find(row.name) if row.file_token is None else None
    if row.file_token is None and drive_id is None:
        return [lark_sheet.FAILED, "", "No audio attachment or Google Drive link in the 'Attachment' cell."]
    try:
        with tempfile.TemporaryDirectory() as folder:
            if drive_id:
                path = link_fetch.download(drive_id, Path(folder))
            else:
                path = download_attachment(row.file_token, Path(row.name).name, Path(folder))
            report = detect_ai.detect(path, active, DETECT_ARGS, escalate=True)
    except UserError as e:
        return [lark_sheet.FAILED, "", str(e)]
    except Exception as e:
        log.exception("row %d (%s) failed", row.number, row.name)
        return [lark_sheet.FAILED, "", UNEXPECTED]
    log_report(f"row {row.number} ({row.name})", report)
    return [VERDICT_TEXT[report["verdict"]], likelihood(report), "\n".join(details(report))]


def handle_batch(message, kind: str, token: str, sheet_id: str | None) -> None:
    target = message.message_id
    job = lark_sheet.plan(client, kind, token, sheet_id)
    prefix = f"Sheet: {job.title}\n"
    with batches_lock:
        if job.token in batches_running:
            reply(target, f"{prefix}I am already checking this sheet. Results appear in the sheet as each row finishes.")
            return
        batches_running.add(job.token)
    try:
        rows, later = job.rows[:BATCH_ROWS], len(job.rows[BATCH_ROWS:])
        skipped = f" {job.already_done} row(s) already have a result and are skipped." if job.already_done else ""
        if not rows:
            nothing = skipped or " No row has a recording in the 'Attachment' column."
            reply(target, f"{prefix}Nothing to check.{nothing}")
            return
        reply(
            target,
            f"{prefix}Checking {len(rows)} recording(s). Each result is written to the "
            f"'{lark_sheet.OUTPUTS[0]}', '{lark_sheet.OUTPUTS[1]}' and '{lark_sheet.OUTPUTS[2]}' columns as it finishes.{skipped}",
        )
        counts: Counter = Counter()
        for row in rows:
            cells = check_row(row)
            lark_sheet.write(client, job, row.number, cells)
            counts[cells[0]] += 1
        log.info("sheet %s: %s", job.title, dict(counts))
        summary = ", ".join(f"{label} {counts[label]}" for label in ("Real", "AI", "Needs review", "Failed") if counts[label])
        more = f"\n{later} more row(s) are waiting; send me the link again to continue." if later else ""
        retry = "\nSend me the link again to retry the failed rows." if counts["Failed"] else ""
        reply(target, f"{prefix}Done, {len(rows)} checked: {summary}.{retry}{more}")
    finally:
        with batches_lock:
            batches_running.discard(job.token)


def handle(message) -> None:
    target, prefix = message.message_id, ""
    try:
        text = lark_sheet.message_text(message.content) if message.message_type in ("text", "post") else ""
        sheet = lark_sheet.find_link(text)
        if sheet:
            handle_batch(message, *sheet)
            return
        drive_id = link_fetch.find(text)
        if drive_id:
            # The file's name is only known once Google has answered.
            label, fetch = None, lambda folder: link_fetch.download(drive_id, folder)
        else:
            audio = find_audio(message)
            if audio is None:
                reply(target, f"I can't open that link. {link_fetch.SUPPORTED}" if link_fetch.has_other_link(text) else USAGE)
                return
            source_id, message_type, content = audio
            # Voice messages carry no file name; Lark records them as Opus.
            name = Path(content.get("file_name") or f"voice-message.{'opus' if message_type == 'audio' else 'bin'}").name
            # Answer on the file's own message and lead with its name, so each
            # result can be matched to its file when several are checked at once.
            label = display_name(message_type, content, name)
            target, prefix = source_id, f"File: {label}\n"
            fetch = lambda folder: download(source_id, content["file_key"], name, folder)
        with tempfile.TemporaryDirectory() as folder:
            path = fetch(Path(folder))
            if label is None:
                label, prefix = path.name, f"File: {path.name}\n"
            reply(target, f"{prefix}Checking. This usually takes under a minute.")
            report = detect_ai.detect(path, active, DETECT_ARGS, escalate=True)
        log_report(label, report)
        reply(target, format_report(report, label))
    except UserError as e:
        log.warning("message %s: %s", message.message_id, e)
        reply(target, f"{prefix}Check failed: {e}")
    except Exception:
        log.exception("handling message %s failed", message.message_id)
        reply(target, f"{prefix}Check failed: {UNEXPECTED}")


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
