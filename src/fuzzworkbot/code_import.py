"""Parsing and importing giveaway codes, shared by `giveaway-cli codes load|import-csv`
and the bot's /prize import so they apply exactly the same rules."""

import csv
import io
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from fuzzworkbot.db import GiveawayCode, Prize

# Values in an optional "given" CSV column meaning "already handed out, skip this row".
GIVEN_TRUTHY = {"yes", "true", "1", "y", "x", "given"}


class CodeFileError(ValueError):
    """The file couldn't be used at all; the message says why, for the user."""


@dataclass
class ParsedCsv:
    rows: list[tuple[str, str]]  # (prize name, code)
    skipped_given: int
    has_given_column: bool


@dataclass
class ImportResult:
    added: int = 0
    skipped_duplicates: int = 0
    created_prizes: list[str] = field(default_factory=list)
    prize_names: set[str] = field(default_factory=set)


def parse_code_lines(text: str) -> list[str]:
    """One code per line; blank lines and surrounding whitespace ignored."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_code_csv(text: str) -> ParsedCsv:
    """A CSV with a header row holding 'prize' and 'code' columns (any order, any case)
    and optionally 'given'. Rows with a blank prize or code are dropped; rows whose
    'given' value is in GIVEN_TRUTHY are dropped and counted."""
    reader = csv.DictReader(io.StringIO(text))
    columns = {(name or "").strip().lower(): name for name in (reader.fieldnames or [])}
    if "prize" not in columns or "code" not in columns:
        raise CodeFileError(
            "CSV must have 'prize' and 'code' columns in its header row (case-insensitive). "
            f"Found: {list(reader.fieldnames or [])}"
        )
    given_column = columns.get("given")

    rows: list[tuple[str, str]] = []
    skipped_given = 0
    for csv_row in reader:
        prize_name = (csv_row.get(columns["prize"]) or "").strip()
        code = (csv_row.get(columns["code"]) or "").strip()
        if not prize_name or not code:
            continue
        if given_column and (csv_row.get(given_column) or "").strip().lower() in GIVEN_TRUTHY:
            skipped_given += 1
            continue
        rows.append((prize_name, code))
    return ParsedCsv(rows=rows, skipped_given=skipped_given, has_given_column=given_column is not None)


def import_codes(session: Session, rows: Iterable[tuple[str, str]], *, create_missing_prizes: bool) -> ImportResult:
    """Add each (prize name, code), skipping codes that prize already has (including
    repeats within `rows`). Must be called inside a write transaction. With
    create_missing_prizes=False an unknown prize name raises CodeFileError."""
    result = ImportResult()
    prize_ids: dict[str, int] = {}
    existing: dict[int, set[str]] = {}

    for prize_name, code in rows:
        if prize_name not in prize_ids:
            prize_row = session.scalar(select(Prize).where(Prize.name == prize_name))
            if prize_row is None:
                if not create_missing_prizes:
                    raise CodeFileError(f"No prize named '{prize_name}'.")
                prize_row = Prize(name=prize_name)
                session.add(prize_row)
                session.flush()
                result.created_prizes.append(prize_name)
            prize_ids[prize_name] = prize_row.id
            existing[prize_row.id] = set(
                session.scalars(select(GiveawayCode.code).where(GiveawayCode.prize_id == prize_row.id))
            )
            result.prize_names.add(prize_name)

        prize_id = prize_ids[prize_name]
        if code in existing[prize_id]:
            result.skipped_duplicates += 1
            continue
        session.add(GiveawayCode(prize_id=prize_id, code=code))
        existing[prize_id].add(code)
        result.added += 1
    return result
