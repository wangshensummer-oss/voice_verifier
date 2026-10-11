"""Batch checking: find the recordings in a Lark Sheet and write verdicts back to it.

The sheet needs a header row and an "Attachment" column holding one audio
attachment per row; if no column has that name, the only column containing
attachments is used. Results go into the "Voice AI or Real", "AI likelihood"
and "Details" columns, the same fields the bot replies with in a chat; the
ones the sheet lacks are added after its last header.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

import lark_oapi as lark
import requests
from lark_oapi.api.sheets.v3 import GetSpreadsheetRequest, QuerySpreadsheetSheetRequest
from lark_oapi.api.wiki.v2 import GetNodeSpaceRequest

# Header names are compared case-insensitively.
VOICE = "attachment"
OUTPUTS = ("Voice AI or Real", "AI likelihood", "Details")
FAILED = "Failed"
# Bounds on what is read from a sheet; real audition sheets are far smaller.
MAX_COLUMNS, MAX_SHEET_ROWS = 100, 5000

SHEET_LINK = re.compile(r"https?://[\w.-]+/(sheets|wiki)/(\w+)(?:\?[^\s\"'<>\\]*?\bsheet=(\w+))?")
SHARE_HINT = "Open the sheet, choose ... > More > Add document app, and give Voice Verifier edit access."


class UserError(Exception):
    """A failure whose message says what went wrong and what to do, safe to show in the chat."""


@dataclass
class Row:
    number: int  # 1-based row in the sheet
    file_token: str | None
    name: str


@dataclass
class Job:
    token: str
    sheet_id: str
    title: str
    columns: list[int]  # 0-based column of each of OUTPUTS
    rows: list[Row] = field(default_factory=list)
    already_done: int = 0


def message_text(content: str) -> str:
    """A message's content as one searchable string, whatever its message type."""
    try:
        return json.dumps(json.loads(content), ensure_ascii=False)
    except ValueError:
        return content


def find_link(text: str) -> tuple[str, str, str | None] | None:
    """Return (kind, token, sheet_id) for the first Lark sheet or wiki link in the text."""
    match = SHEET_LINK.search(text)
    return match.groups() if match else None


def column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def cell_text(cell) -> str:
    if cell is None:
        return ""
    if isinstance(cell, list):
        return "".join(cell_text(part) for part in cell)
    if isinstance(cell, dict):
        # A hyperlinked cell shows its text but the address is what a link check needs.
        text, link = str(cell.get("text") or ""), str(cell.get("link") or "")
        return text if not link or link in text else f"{text} {link}"
    return str(cell)


def attachments(cell) -> list[tuple[str, str]]:
    """(file_token, name) for every attachment in a cell, however Lark nests them."""
    if isinstance(cell, list):
        return [found for part in cell for found in attachments(part)]
    if isinstance(cell, dict) and cell.get("fileToken"):
        return [(cell["fileToken"], str(cell.get("text") or "recording"))]
    return []


def with_retry(send):
    """Run one Lark request, retrying when the connection fails; the link to Lark drops now and then."""
    for attempt in range(1, 4):
        try:
            return send()
        except requests.RequestException:
            if attempt == 3:
                raise UserError("I lost the connection to Lark. Please try again in a minute.")
            time.sleep(2 * attempt)


def call(client: lark.Client, method: lark.HttpMethod, uri: str, body: dict | None = None) -> dict:
    """Call a Lark endpoint the SDK has no typed wrapper for and return its data."""
    request = (
        lark.BaseRequest.builder()
        .http_method(method)
        .uri(uri)
        .token_types({lark.AccessTokenType.TENANT})
        .body(body)
        .build()
    )
    raw = with_retry(lambda: client.request(request)).raw
    try:
        payload = json.loads(raw.content)
    except ValueError:
        raise UserError(f"Lark returned HTTP {raw.status_code} for the sheet. Please try again in a minute.")
    if payload.get("code") != 0:
        code, message = payload.get("code"), str(payload.get("msg"))
        if "forbidden" in message.lower() or "permission" in message.lower():
            raise UserError(f"Lark refused access to the sheet ({code}: {message}). {SHARE_HINT}")
        raise UserError(f"Lark rejected the sheet request ({code}: {message}). Please tell the bot's maintainer.")
    return payload.get("data") or {}


def plan(client: lark.Client, kind: str, token: str, sheet_id: str | None) -> Job:
    """Read the sheet and list the rows that still need a verdict."""
    if kind == "wiki":
        response = with_retry(lambda: client.wiki.v2.space.get_node(GetNodeSpaceRequest.builder().token(token).build()))
        if not response.success():
            raise UserError(f"I could not open that wiki page ({response.code}: {response.msg}). {SHARE_HINT}")
        node = response.data.node
        if node.obj_type != "sheet":
            raise UserError(f"That wiki page is of type '{node.obj_type}', not a sheet. Send me the link to a Lark Sheet.")
        token = node.obj_token

    response = with_retry(
        lambda: client.sheets.v3.spreadsheet_sheet.query(
            QuerySpreadsheetSheetRequest.builder().spreadsheet_token(token).build()
        )
    )
    if not response.success():
        raise UserError(f"I could not open that sheet ({response.code}: {response.msg}). {SHARE_HINT}")
    sheets = response.data.sheets or []
    sheet = next((s for s in sheets if s.sheet_id == sheet_id), sheets[0] if sheets else None)
    if sheet is None or sheet.grid_properties is None:
        raise UserError("That spreadsheet has no grid sheet to read.")

    columns = min(sheet.grid_properties.column_count or 0, MAX_COLUMNS)
    last_row = min(sheet.grid_properties.row_count or 0, MAX_SHEET_ROWS)
    area = f"{sheet.sheet_id}!A1:{column_letter(columns - 1)}{last_row}"
    data = call(client, lark.HttpMethod.GET, f"/open-apis/sheets/v2/spreadsheets/{token}/values/{area}")
    values = data.get("valueRange", {}).get("values") or []
    if not values:
        raise UserError("That sheet is empty.")

    header = [cell_text(cell).strip().lower() for cell in values[0]]
    if VOICE in header:
        voice = header.index(VOICE)
    else:
        # Without a column of that name, a single column of attachments is unambiguous.
        holding = {i for row in values[1:] for i, cell in enumerate(row) if attachments(cell)}
        if len(holding) != 1:
            raise UserError(
                f"The sheet '{sheet.title}' has no 'Attachment' column. Put the recordings in a column named 'Attachment'."
            )
        voice = holding.pop()

    # Missing output columns go after everything already in the sheet, so nothing is overwritten.
    last_used = max((i for i, name in enumerate(header) if name), default=0)
    places = []
    for name in OUTPUTS:
        if name.lower() in header:
            places.append(header.index(name.lower()))
            continue
        last_used += 1
        if last_used >= columns:
            raise UserError(f"There is no free column for '{name}'. Add a column named '{name}' and try again.")
        places.append(last_used)

    # Name the spreadsheet in replies, not the tab, which is usually just "Sheet1".
    info = with_retry(
        lambda: client.sheets.v3.spreadsheet.get(GetSpreadsheetRequest.builder().spreadsheet_token(token).build())
    )
    title = info.data.spreadsheet.title if info.success() else sheet.title
    job = Job(token, sheet.sheet_id, title, places)
    for index, name in zip(places, OUTPUTS):
        if name.lower() not in header:
            put(client, job, f"{column_letter(index)}1:{column_letter(index)}1", [[name]])
    verdict = places[0]
    for number, row in enumerate(values[1:], start=2):
        voice_cell = row[voice] if voice < len(row) else None
        if not cell_text(voice_cell).strip() and not attachments(voice_cell):
            continue
        existing = cell_text(row[verdict] if verdict < len(row) else None).strip()
        # Rows that failed last time are tried again.
        if existing and not existing.startswith(FAILED):
            job.already_done += 1
            continue
        found = attachments(voice_cell)
        job.rows.append(Row(number, *(found[0] if found else (None, cell_text(voice_cell).strip()))))
    return job


def put(client: lark.Client, job: Job, cells: str, values: list[list[str]]) -> None:
    body = {"valueRange": {"range": f"{job.sheet_id}!{cells}", "values": values}}
    call(client, lark.HttpMethod.PUT, f"/open-apis/sheets/v2/spreadsheets/{job.token}/values", body)


def write(client: lark.Client, job: Job, row: int, values: list[str]) -> None:
    """Write one row's OUTPUTS values, in a single request when their columns are adjacent."""
    first, last = column_letter(job.columns[0]), column_letter(job.columns[-1])
    if job.columns == list(range(job.columns[0], job.columns[0] + len(job.columns))):
        put(client, job, f"{first}{row}:{last}{row}", [values])
        return
    for index, value in zip(job.columns, values):
        letter = column_letter(index)
        put(client, job, f"{letter}{row}:{letter}{row}", [[value]])
