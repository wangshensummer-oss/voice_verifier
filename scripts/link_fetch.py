"""Download the recording behind a shared link.

Only known file-sharing services are fetched, never an arbitrary URL: the bot
runs on a server, and fetching whatever address a chat message names would let
anyone make that server call internal or private addresses.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

from lark_sheet import UserError

MAX_BYTES = 200 * 1024 * 1024
ANY_LINK = re.compile(r"https?://[^\s\"'<>\\]+")
GOOGLE_DRIVE_FILE = re.compile(
    r"https?://(?:drive|docs)\.google\.com/(?:file/d/|open\?(?:[^\s\"'<>\\]*&)?id=|uc\?(?:[^\s\"'<>\\]*&)?id=)([\w-]{10,})"
)
GOOGLE_HOSTS = (".google.com", ".googleusercontent.com")
SUPPORTED = "I can open Google Drive file links and Lark Sheet links."

http = requests.Session()
http.trust_env = False  # connect directly, like detect_ai


def find(text: str) -> str | None:
    """Return the Google Drive file ID in a message or cell, if it holds a supported link."""
    match = GOOGLE_DRIVE_FILE.search(text)
    return match.group(1) if match else None


def has_other_link(text: str) -> bool:
    return bool(ANY_LINK.search(text))


def file_name(response: requests.Response, fallback: str) -> str:
    disposition = response.headers.get("Content-Disposition", "")
    encoded = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", disposition, re.I)
    plain = re.search(r'filename="?([^";]+)"?', disposition, re.I)
    name = unquote(encoded.group(1)) if encoded else plain.group(1) if plain else fallback
    return Path(name.strip()).name or fallback


def download(file_id: str, folder: Path) -> Path:
    """Download a Google Drive file that is shared with anyone who has the link."""
    try:
        response = http.get(
            "https://drive.usercontent.google.com/download",
            params={"id": file_id, "export": "download", "confirm": "t"},
            stream=True,
            timeout=60,
        )
    except requests.RequestException:
        raise UserError("I could not reach Google Drive. Please try again in a minute.")
    with response:
        host = urlparse(response.url).hostname or ""
        if not host.endswith(GOOGLE_HOSTS):
            raise UserError("That Google Drive link led somewhere else, so I did not open it.")
        # A file that is not public, or not a file at all, comes back as a web page.
        if response.status_code != 200 or response.headers.get("Content-Type", "").startswith("text/html"):
            raise UserError(
                "I could not download that Google Drive file. Set its sharing to 'Anyone with the link' "
                "and make sure the link points to a single audio file, not a folder."
            )
        path = folder / file_name(response, "recording")
        size = 0
        with path.open("wb") as out:
            for chunk in response.iter_content(1024 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise UserError(f"That file is larger than {MAX_BYTES // (1024 * 1024)} MB, which is more than I check.")
                out.write(chunk)
    return path
