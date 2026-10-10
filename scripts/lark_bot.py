#!/usr/bin/env python3
"""Lark bot that runs AI-speech detection on audio files sent to it.

  Direct chat: send the bot an audio file.
  Group chat:  reply to the audio file's message and @ the bot.

Connects out to Lark over a long connection, so no public URL is needed.
Needs LARK_APP_ID and LARK_APP_SECRET in the environment / .env, plus at least
one detector key (see detect_ai.py). Set LARK_DOMAIN=feishu for Feishu tenants.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import detect_ai

detect_ai.load_env()
# The SDK would otherwise pick up an ambient proxy; connect directly like detect_ai does.
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"

import lark_oapi as lark
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
    "FLAG": "疑似 AI 合成",
    "REVIEW": "需要人工复核",
    "PASS": "未发现 AI 合成迹象",
    "PASS (single detector)": "未发现 AI 合成迹象（仅一家检测器）",
}
USAGE = "请直接把音频文件发给我；在群里请回复那条音频文件消息并 @我。"
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


def find_audio(message) -> tuple[str, str, dict] | None:
    """Return (message_id, message_type, content) of the audio this message carries or replies to."""
    if message.message_type in AUDIO_MESSAGE_TYPES:
        return message.message_id, message.message_type, json.loads(message.content)
    if not message.parent_id:
        return None
    response = client.im.v1.message.get(GetMessageRequest.builder().message_id(message.parent_id).build())
    if not response.success():
        log.error("get parent message failed: %s %s", response.code, response.msg)
        return None
    parent = response.data.items[0]
    if parent.msg_type not in AUDIO_MESSAGE_TYPES:
        return None
    return parent.message_id, parent.msg_type, json.loads(parent.body.content)


def download(message_id: str, file_key: str, name: str, folder: Path) -> Path:
    request = GetMessageResourceRequest.builder().message_id(message_id).file_key(file_key).type("file").build()
    response = client.im.v1.message_resource.get(request)
    if not response.success():
        raise RuntimeError(f"下载文件失败：{response.code} {response.msg}")
    path = folder / name
    path.write_bytes(response.file.read())
    return path


def format_report(report: dict, name: str) -> str:
    lines = [f"检测结论：{VERDICT_TEXT[report['verdict']]}", f"文件：{name}"]
    for provider, r in report["providers"].items():
        score = "" if r["score"] is None else f"，AI 概率 {r['score']:.2f}"
        lines.append(f"· {provider}：{r['label']}{score}（{r['detail']}）")
    lines.append("以上是复核信号，不是定论。")
    return "\n".join(lines)


def handle(message) -> None:
    try:
        audio = find_audio(message)
        if audio is None:
            reply(message.message_id, USAGE)
            return
        source_id, message_type, content = audio
        # Voice messages carry no file name; Lark records them as Opus.
        name = Path(content.get("file_name") or f"voice-message.{'opus' if message_type == 'audio' else 'bin'}").name
        reply(message.message_id, f"收到 {name}，检测中，通常需要不到一分钟。")
        with tempfile.TemporaryDirectory() as folder:
            path = download(source_id, content["file_key"], name, Path(folder))
            report = detect_ai.detect(path, active, DETECT_ARGS)
        log.info("%s -> %s", name, report["verdict"])
        reply(message.message_id, format_report(report, name))
    except Exception as e:
        log.exception("handling message %s failed", message.message_id)
        reply(message.message_id, f"检测失败：{e}")


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


def main() -> int:
    global client, active
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
