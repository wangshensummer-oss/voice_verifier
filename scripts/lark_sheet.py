"""Batch checking: find the recordings in a Lark Sheet and write verdicts back to it.

The sheet needs a header row and a "voice" column holding one audio
attachment per row; if no column has that name, the only column containing
attachments is used. Verdicts go into the "conclusion" column, which is added
after the last header if the sheet has none.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import lark_oapi as lark
from lark_oapi.api.sheets.v3 import QuerySpreadsheetSheetRequest
from lark_oapi.api.wiki.v2 import GetNodeSpaceRequest

VOICE, CONCLUSION = "voice", "conclusion"
FAILED_PREFIX = "Failed:"
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
    conclusion_column: str
    rows: list[Row] = field(default_factory=list)
    already_done: int = 0


def find_link(content: str) -> tuple[str, str, str | None] | None:
    """Return (kind, token, sheet_id) for the first sheet or wiki link in a message's content."""
    try:
        text = json.dumps(json.loads(content), ensure_ascii=False)
    except ValueError:
        text = content
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
        return str(cell.get("text") or "")
    return str(cell)


def attachments(cell) -> list[tuple[str, str]]:
    """(file_token, name) for every attachment in a cell, however Lark nests them."""
    if isinstance(cell, list):
        return [found for part in cell for found in attachments(part)]
    if isinstance(cell, dict) and cell.get("fileToken"):
        return [(cell["fileToken"], str(cell.get("text") or "recording"))]
    return []


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
    raw = client.request(request).raw
    try:
        payload = json.loads(raw.content)
    except ValueError:
        raise UserError(f"Lark returned HTTP {raw.status_code} for the sheet. Please try again in a minute.")
    if payload.get("code") != 0:
        raise UserError(f"Lark refused access to the sheet ({payload.get('code')}: {payload.get('msg')}). {SHARE_HINT}")
    return payload.get("data") or {}


def plan(client: lark.Client, kind: str, token: str, sheet_id: str | None) -> Job:
    """Read the sheet and list the rows that still need a verdict."""
    if kind == "wiki":
        response = client.wiki.v2.space.get_node(GetNodeSpaceRequest.builder().token(token).build())
        if not response.success():
            raise UserError(f"I could not open that wiki page ({response.code}: {response.msg}). {SHARE_HINT}")
        node = response.data.node
        if node.obj_type != "sheet":
            raise UserError(f"That wiki page is of type '{node.obj_type}', not a sheet. Send me the link to a Lark Sheet.")
        token = node.obj_token

    response = client.sheets.v3.spreadsheet_sheet.query(
        QuerySpreadsheetSheetRequest.builder().spreadsheet_token(token).build()
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
                f"The sheet '{sheet.title}' has no '{VOICE}' column. Name the column with the recordings '{VOICE}'."
            )
        voice = holding.pop()
    if CONCLUSION in header:
        conclusion = header.index(CONCLUSION)
    else:
        conclusion = max((i for i, name in enumerate(header) if name), default=0) + 1
        if conclusion >= columns:
            raise UserError(f"There is no free column for '{CONCLUSION}'. Add a column named '{CONCLUSION}' and try again.")

    job = Job(token, sheet.sheet_id, sheet.title, column_letter(conclusion))
    if CONCLUSION not in header:
        write(client, job, 1, CONCLUSION)
    for number, row in enumerate(values[1:], start=2):
        voice_cell = row[voice] if voice < len(row) else None
        if not cell_text(voice_cell).strip() and not attachments(voice_cell):
            continue
        existing = cell_text(row[conclusion] if conclusion < len(row) else None).strip()
        # Rows that failed last time are tried again.
        if existing and not existing.startswith(FAILED_PREFIX):
            job.already_done += 1
            continue
        found = attachments(voice_cell)
        job.rows.append(Row(number, *(found[0] if found else (None, cell_text(voice_cell).strip()))))
    return job


def write(client: lark.Client, job: Job, row: int, text: str) -> None:
    cell = f"{job.sheet_id}!{job.conclusion_column}{row}:{job.conclusion_column}{row}"
    body = {"valueRange": {"range": cell, "values": [[text]]}}
    call(client, lark.HttpMethod.PUT, f"/open-apis/sheets/v2/spreadsheets/{job.token}/values", body)
