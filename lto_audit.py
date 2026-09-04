#!/usr/bin/env python3
"""
Read-only audit of YoYotta PDF manifests against files stored under one or more
Linux roots.

The source trees are never modified. The program writes only to --out-dir (or
--db for individual stages). For scan/all, source mounts are required to be
read-only unless --allow-rw-source is explicitly supplied.

Python: 3.9+
Optional PDF backends:
  1. PyMuPDF (recommended; pip install PyMuPDF)
  2. pdftotext from poppler-utils (automatic fallback)
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import itertools
import json
import math
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import unicodedata
import uuid
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import quote

VERSION = "1.5.0"
CENTRAL_SCHEMA_VERSION = 2
SNAPSHOT_SCHEMA_VERSION = 1

BINARY_UNITS = {
    "B": 1,
    "KB": 1024,
    "MB": 1024 ** 2,
    "GB": 1024 ** 3,
    "TB": 1024 ** 4,
}

DECIMAL_UNITS = {
    "B": 1,
    "KB": 1000,
    "MB": 1000 ** 2,
    "GB": 1000 ** 3,
    "TB": 1000 ** 4,
}

PDF_SIZE_UNIT_TABLES = {
    "decimal": DECIMAL_UNITS,
    "binary": BINARY_UNITS,
}

ENTRY_RE = re.compile(
    r"^(Name|First)\s*:\s*(.*?)\s+Size\s*:\s*"
    r"([0-9]+(?:\.[0-9]+)?)\s*(B|KB|MB|GB|TB)\s*$"
)
LAST_RE = re.compile(
    r"^Last\s*:\s*(.*?)\s+Size\s*:\s*"
    r"([0-9]+(?:\.[0-9]+)?)\s*(B|KB|MB|GB|TB)\s*$"
)
PATH_RE = re.compile(r"^Path\s*:\s*(/Volumes/.*)$")
TOTAL_FILES_RE = re.compile(r"Total\s+Files\s*:\s*(\d+)", re.IGNORECASE)
PROJECT_RE = re.compile(r"Project\s*:\s*(.*?)\s{2,}(?:Collection\s*:|$)", re.IGNORECASE)
FRAMES_RE = re.compile(r"Frames\s*:\s*(\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class SizeRange:
    display: str
    minimum: Optional[int]
    maximum: Optional[int]
    known: bool


@dataclass
class LtoEntry:
    source_pdf: str
    source_page: int
    project: str
    tape: str
    relative_path: str
    norm_path: str
    top_folder: str
    norm_top_folder: str
    filename: str
    norm_filename: str
    reported_size: str
    size_min_bytes: Optional[int]
    size_max_bytes: Optional[int]
    size_known: int
    entry_kind: str
    sequence_id: str


@dataclass
class StorageEntry:
    source_root: str
    absolute_path: str
    absolute_path_bytes: bytes
    relative_path: str
    relative_path_bytes: bytes
    path_encoding_valid: int
    norm_path: str
    top_folder: str
    norm_top_folder: str
    filename: str
    norm_filename: str
    size_bytes: int
    mtime_ns: int


@dataclass
class StorageClassification:
    """Final comparison result for one physical storage entry.

    ``status`` is suitable for the legacy reports.  The additional facts keep
    safety decisions explicit instead of inferring them from a path-level CSV
    row, which may represent files from more than one storage root.
    """

    storage: StorageEntry
    status: str
    lto_entries: List[LtoEntry]
    matched_lto_entries: List[LtoEntry]
    note: str
    verified_known_size_full_path: bool
    lto_duplicate_kind: str
    storage_duplicate_kind: str
    blocking_reasons: Tuple[str, ...]


@dataclass
class LtoSizeTotals:
    path_count: int = 0
    known_size_count: int = 0
    unknown_size_count: int = 0
    conflicting_size_path_count: int = 0
    additional_identical_tape_copy_count: int = 0
    total_min_bytes: int = 0
    total_max_bytes: int = 0


@dataclass
class FolderAudit:
    source_root: str
    top_folder_bytes: bytes
    top_folder: str
    norm_top_folder: str
    classifications: List[StorageClassification]
    blocking_reasons: List[str]
    scan_issue_types: List[str]
    matched_lto_totals: LtoSizeTotals
    manifest_lto_totals: LtoSizeTotals
    storage_norm_paths: set[str]
    manifest_norm_paths: set[str]


@dataclass(frozen=True)
class PdfSizeUnitInfo:
    mode: str
    label: str
    warning: str = ""


@dataclass(frozen=True)
class SimpleFolderEvaluation:
    result: str
    reason_codes: Tuple[str, ...]
    known_size_match_count: int
    unknown_size_path_match_count: int
    storage_only_path_count: int
    lto_only_path_count: int
    size_mismatch_count: int
    conflicting_lto_path_count: int
    ambiguous_path_count: int
    zero_size_file_count: int
    scan_issue_count: int
    invalid_encoding_count: int
    aggregate_size_result: str


@dataclass(frozen=True)
class DiscoveryAnchor:
    relative_below_top: str
    norm_path: str
    filename: str
    size_min_bytes: Optional[int]
    size_max_bytes: Optional[int]
    size_known: bool
    distinctive_below_top: bool


@dataclass(frozen=True)
class SourceDirectory:
    raid_root: Path
    project_name: str
    path: Path


@dataclass(frozen=True)
class DiscoveryCandidate:
    source: SourceDirectory
    physical_folder: Path


@dataclass(frozen=True)
class DiscoveryCandidateScore:
    candidate: DiscoveryCandidate
    anchors_tested: int
    anchors_path_matched: int
    anchors_size_matched: int
    anchors_size_mismatched: int
    unknown_size_paths_matched: int


@dataclass(frozen=True)
class MappedScanRoot:
    source_directory: Path
    physical_folder: Path
    lto_top_folder: str


def eprint(*args: object, **kwargs: object) -> None:
    print(*args, file=sys.stderr, **kwargs)


def has_surrogateescape(value: str) -> bool:
    """Return True when a filesystem string contains undecodable raw bytes."""
    return any(0xDC80 <= ord(char) <= 0xDCFF for char in value)


def safe_filesystem_display(value: str) -> str:
    """Make an arbitrary Unix filename safe for UTF-8 SQLite/CSV output.

    Python represents non-UTF-8 filename bytes as U+DC80..U+DCFF via
    surrogateescape. SQLite's TEXT interface correctly refuses those code
    points, so display them as explicit ``\\xNN`` escapes. Exact raw bytes are
    stored separately as BLOB values and are used for NUL-delimited copy lists.
    """
    pieces: List[str] = []
    for char in value:
        code = ord(char)
        if 0xDC80 <= code <= 0xDCFF:
            pieces.append(f"\\x{code - 0xDC00:02X}")
        else:
            pieces.append(char)
    return "".join(pieces)


def raw_norm_key(raw_relative_path: bytes) -> str:
    """Stable key for a path that is not valid UTF-8; never matches PDF text."""
    return "__NON_UTF8_RAW_BYTES__/" + raw_relative_path.hex()


def normalize_component(value: str) -> str:
    """Case-insensitive, Unicode-stable comparison key without trimming names."""
    return unicodedata.normalize("NFC", value).casefold()


def normalize_relative_path(value: str) -> str:
    value = value.replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    value = value.lstrip("/")
    parts = [part for part in value.split("/") if part not in ("", ".")]
    return "/".join(normalize_component(part) for part in parts)


def path_parts(relative_path: str) -> Tuple[str, str]:
    parts = PurePosixPath(relative_path).parts
    top = parts[0] if parts else ""
    filename = parts[-1] if parts else ""
    return top, filename


def parse_lto_path(raw_path: str) -> Tuple[str, str]:
    """Return (tape_name, path relative to /Volumes/<tape>/)."""
    path = PurePosixPath(raw_path)
    parts = path.parts
    # ('/', 'Volumes', 'CTH348', '16-12-2024', ...)
    if len(parts) < 4 or parts[1] != "Volumes":
        raise ValueError(f"Unexpected YoYotta path: {raw_path!r}")
    tape = parts[2]
    relative = PurePosixPath(*parts[3:]).as_posix()
    if not relative or relative == ".":
        raise ValueError(f"YoYotta path has no relative file path: {raw_path!r}")
    return tape, relative


def size_interval(number_text: str, unit: str, *, unit_mode: str = "binary") -> SizeRange:
    """Convert a rounded YoYotta value to the exact integer-byte interval.

    ``unit_mode`` controls whether PDF labels such as GB mean decimal SI units
    or binary powers. Any integer byte count that rounds to the displayed value
    at the shown precision is accepted.
    """
    try:
        value = Decimal(number_text)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid size number: {number_text!r}") from exc

    try:
        multiplier = PDF_SIZE_UNIT_TABLES[unit_mode][unit]
    except KeyError as exc:
        if unit_mode not in PDF_SIZE_UNIT_TABLES:
            raise ValueError(f"Unsupported PDF size unit mode: {unit_mode!r}") from exc
        raise ValueError(f"Unsupported PDF size unit: {unit!r}") from exc
    display = f"{number_text} {unit}"

    if unit == "B":
        if value != value.to_integral_value():
            raise ValueError(f"Fractional byte count is unsupported: {display}")
        exact = int(value)
        return SizeRange(display, exact, exact, True)

    # Precision follows the number of displayed decimal places.
    decimals = max(0, -value.as_tuple().exponent)
    step = Decimal(1).scaleb(-decimals)  # 0.01 for two decimals
    half_step = step / 2
    lower_real = max(Decimal(0), (value - half_step) * multiplier)
    upper_real = (value + half_step) * multiplier

    minimum = int(lower_real.to_integral_value(rounding=ROUND_CEILING))
    # upper edge is exclusive: ceil(edge) - 1 gives the largest integer below it.
    maximum = int(upper_real.to_integral_value(rounding=ROUND_CEILING)) - 1
    maximum = max(minimum, maximum)
    return SizeRange(display, minimum, maximum, True)


def unknown_size() -> SizeRange:
    return SizeRange("", None, None, False)


def size_matches(storage_bytes: int, lto: LtoEntry) -> bool:
    if not lto.size_known:
        return True
    assert lto.size_min_bytes is not None and lto.size_max_bytes is not None
    return lto.size_min_bytes <= storage_bytes <= lto.size_max_bytes


def expand_numeric_sequence(first: str, last: str, frames: int) -> Optional[List[str]]:
    """Expand names differing by exactly one numeric run.

    Examples:
      A001.png -> A003.png
      copter card1.png -> copter card2.png
    """
    first_runs = list(re.finditer(r"\d+", first))
    last_runs = list(re.finditer(r"\d+", last))
    candidates: List[Tuple[re.Match[str], int, int, int]] = []

    if len(first_runs) != len(last_runs):
        return None

    for fm, lm in zip(first_runs, last_runs):
        if first[: fm.start()] != last[: lm.start()]:
            continue
        if first[fm.end() :] != last[lm.end() :]:
            continue
        start = int(fm.group(0))
        end = int(lm.group(0))
        if end < start or end - start + 1 != frames:
            continue
        width = max(len(fm.group(0)), len(lm.group(0)))
        candidates.append((fm, start, end, width))

    if len(candidates) != 1:
        return None

    match, start, end, width = candidates[0]
    return [
        first[: match.start()] + str(number).zfill(width) + first[match.end() :]
        for number in range(start, end + 1)
    ]


def extract_pdf_lines(pdf_path: Path) -> List[Tuple[int, str]]:
    """Return (page_number, stripped_line), preferring PyMuPDF."""
    try:
        import fitz  # type: ignore

        doc = fitz.open(str(pdf_path))
        lines: List[Tuple[int, str]] = []
        try:
            for page_no, page in enumerate(doc, start=1):
                text = page.get_text("text", sort=True)
                lines.extend((page_no, line.strip()) for line in text.splitlines())
        finally:
            doc.close()
        return lines
    except ImportError:
        pass

    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        raise RuntimeError(
            "No PDF backend found. Install PyMuPDF in the venv "
            "(`pip install PyMuPDF`) or install poppler-utils/pdftotext."
        )

    proc = subprocess.run(
        [pdftotext, "-layout", str(pdf_path), "-"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    text = proc.stdout.decode("utf-8", errors="replace")
    lines = []
    pages = text.split("\f")
    for page_no, page_text in enumerate(pages, start=1):
        lines.extend((page_no, line.strip()) for line in page_text.splitlines())
    return lines


def find_project(lines: Sequence[Tuple[int, str]], fallback: str) -> str:
    for _, line in lines[:100]:
        match = PROJECT_RE.search(line)
        if match:
            project = match.group(1).strip()
            if project:
                return project
        if line.startswith("Project :"):
            tail = line[len("Project :") :].strip()
            if tail:
                return re.split(r"\s{2,}Collection\s*:", tail)[0].strip()
    return fallback


def find_expected_files(lines: Sequence[Tuple[int, str]]) -> Optional[int]:
    for _, line in lines[:100]:
        match = TOTAL_FILES_RE.search(line)
        if match:
            return int(match.group(1))
    return None


def find_preceding_frames(
    lines: Sequence[Tuple[int, str]], index: int, lookback: int = 24
) -> Optional[int]:
    for pos in range(index - 1, max(-1, index - lookback - 1), -1):
        match = FRAMES_RE.search(lines[pos][1])
        if match:
            return int(match.group(1))
    return None


def make_lto_entry(
    *,
    source_pdf: str,
    source_page: int,
    project: str,
    tape: str,
    relative_path: str,
    size: SizeRange,
    entry_kind: str,
    sequence_id: str = "",
) -> LtoEntry:
    top, filename = path_parts(relative_path)
    return LtoEntry(
        source_pdf=source_pdf,
        source_page=source_page,
        project=project,
        tape=tape,
        relative_path=relative_path,
        norm_path=normalize_relative_path(relative_path),
        top_folder=top,
        norm_top_folder=normalize_component(top),
        filename=filename,
        norm_filename=normalize_component(filename),
        reported_size=size.display,
        size_min_bytes=size.minimum,
        size_max_bytes=size.maximum,
        size_known=int(size.known),
        entry_kind=entry_kind,
        sequence_id=sequence_id,
    )


def parse_yoyotta_pdf(
    pdf_path: Path, *, pdf_size_units: str = "decimal"
) -> Tuple[List[LtoEntry], List[dict], dict]:
    lines = extract_pdf_lines(pdf_path)
    project = find_project(lines, pdf_path.stem)
    expected_files = find_expected_files(lines)
    entries: List[LtoEntry] = []
    issues: List[dict] = []
    sequence_counter = 0

    index = 0
    while index < len(lines):
        page_no, line = lines[index]
        match = ENTRY_RE.match(line)
        if not match:
            index += 1
            continue

        kind, first_name, number_text, unit = match.groups()

        if kind == "Name":
            raw_path: Optional[str] = None
            path_index: Optional[int] = None
            for probe in range(index + 1, min(index + 14, len(lines))):
                path_match = PATH_RE.match(lines[probe][1])
                if path_match:
                    raw_path = path_match.group(1)
                    path_index = probe
                    break
                if ENTRY_RE.match(lines[probe][1]):
                    break

            if raw_path is None or path_index is None:
                issues.append(
                    {
                        "source_pdf": str(pdf_path),
                        "page": page_no,
                        "issue_type": "record_without_path",
                        "details": line,
                    }
                )
                index += 1
                continue

            try:
                tape, relative_path = parse_lto_path(raw_path)
                size = size_interval(number_text, unit, unit_mode=pdf_size_units)
                entry = make_lto_entry(
                    source_pdf=str(pdf_path),
                    source_page=page_no,
                    project=project,
                    tape=tape,
                    relative_path=relative_path,
                    size=size,
                    entry_kind="regular",
                )
                if normalize_component(first_name) != normalize_component(entry.filename):
                    issues.append(
                        {
                            "source_pdf": str(pdf_path),
                            "page": page_no,
                            "issue_type": "name_path_disagreement",
                            "details": json.dumps(
                                {
                                    "name": first_name,
                                    "path_filename": entry.filename,
                                    "path": raw_path,
                                },
                                ensure_ascii=False,
                            ),
                        }
                    )
                entries.append(entry)
            except Exception as exc:
                issues.append(
                    {
                        "source_pdf": str(pdf_path),
                        "page": page_no,
                        "issue_type": "record_parse_error",
                        "details": f"{line} | {exc}",
                    }
                )
            index = path_index + 1
            continue

        # First/Last image sequence.
        last_data: Optional[Tuple[str, str, str]] = None
        raw_path = None
        path_index = None
        for probe in range(index + 1, min(index + 18, len(lines))):
            last_match = LAST_RE.match(lines[probe][1])
            if last_match:
                last_data = last_match.groups()
            path_match = PATH_RE.match(lines[probe][1])
            if path_match:
                raw_path = path_match.group(1)
                path_index = probe
                break

        frames = find_preceding_frames(lines, index)
        if last_data is None or raw_path is None or path_index is None or frames is None:
            issues.append(
                {
                    "source_pdf": str(pdf_path),
                    "page": page_no,
                    "issue_type": "incomplete_sequence_record",
                    "details": json.dumps(
                        {
                            "first_line": line,
                            "last": last_data,
                            "path": raw_path,
                            "frames": frames,
                        },
                        ensure_ascii=False,
                    ),
                }
            )
            index += 1
            continue

        last_name, last_number, last_unit = last_data
        expanded_names = expand_numeric_sequence(first_name, last_name, frames)
        if not expanded_names:
            issues.append(
                {
                    "source_pdf": str(pdf_path),
                    "page": page_no,
                    "issue_type": "sequence_not_expandable",
                    "details": json.dumps(
                        {
                            "first": first_name,
                            "last": last_name,
                            "frames": frames,
                            "path": raw_path,
                        },
                        ensure_ascii=False,
                    ),
                }
            )
            index = path_index + 1
            continue

        try:
            tape, first_relative = parse_lto_path(raw_path)
            parent = PurePosixPath(first_relative).parent
            first_size = size_interval(number_text, unit, unit_mode=pdf_size_units)
            last_size = size_interval(last_number, last_unit, unit_mode=pdf_size_units)
            sequence_counter += 1
            sequence_id = f"{pdf_path.name}:p{page_no}:seq{sequence_counter}"

            for seq_index, filename in enumerate(expanded_names):
                relative_path = (parent / filename).as_posix()
                if seq_index == 0:
                    size = first_size
                    entry_kind = "sequence_first"
                elif seq_index == len(expanded_names) - 1:
                    size = last_size
                    entry_kind = "sequence_last"
                else:
                    size = unknown_size()
                    entry_kind = "sequence_middle_size_unknown"
                    issues.append(
                        {
                            "source_pdf": str(pdf_path),
                            "page": page_no,
                            "issue_type": "sequence_file_size_unknown",
                            "details": json.dumps(
                                {
                                    "path": relative_path,
                                    "sequence_id": sequence_id,
                                    "first": first_name,
                                    "last": last_name,
                                    "frames": frames,
                                },
                                ensure_ascii=False,
                            ),
                        }
                    )

                entries.append(
                    make_lto_entry(
                        source_pdf=str(pdf_path),
                        source_page=page_no,
                        project=project,
                        tape=tape,
                        relative_path=relative_path,
                        size=size,
                        entry_kind=entry_kind,
                        sequence_id=sequence_id,
                    )
                )
        except Exception as exc:
            issues.append(
                {
                    "source_pdf": str(pdf_path),
                    "page": page_no,
                    "issue_type": "sequence_parse_error",
                    "details": f"{line} | {exc}",
                }
            )
        index = path_index + 1

    stats = {
        "source_pdf": str(pdf_path),
        "project": project,
        "expected_files": expected_files,
        "extracted_files": len(entries),
        "difference": None if expected_files is None else len(entries) - expected_files,
        "sequence_files": sum(1 for entry in entries if entry.sequence_id),
        "size_unknown_files": sum(1 for entry in entries if not entry.size_known),
        "issues": len(issues),
    }
    if expected_files is not None and expected_files != len(entries):
        issues.append(
            {
                "source_pdf": str(pdf_path),
                "page": 0,
                "issue_type": "total_files_mismatch",
                "details": json.dumps(stats, ensure_ascii=False),
            }
        )
        stats["issues"] = len(issues)
    return entries, issues, stats


def connect_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA temp_store=MEMORY")
    initialize_schema(connection)
    return connection


def connect_db_readonly(db_path: Path) -> sqlite3.Connection:
    """Open an existing audit database without creating or changing it."""
    db_path = db_path.expanduser().resolve()
    if not db_path.is_file():
        raise FileNotFoundError(f"Audit database not found: {db_path}")
    sidecars = [Path(str(db_path) + suffix) for suffix in ("-wal", "-shm")]
    present_sidecars = [path for path in sidecars if path.exists()]
    if present_sidecars:
        raise RuntimeError(
            "Audit database has active SQLite WAL/SHM sidecars and cannot be opened "
            "as an immutable read-only snapshot: "
            + ", ".join(str(path) for path in present_sidecars)
        )
    # immutable=1 prevents SQLite from creating WAL/SHM sidecars for this
    # reporting-only connection. Audit databases are completed snapshots and
    # must not be concurrently written while this command is running.
    uri = "file:" + quote(str(db_path), safe="/") + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current_version > CENTRAL_SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {current_version} is newer than supported "
            f"version {CENTRAL_SCHEMA_VERSION}."
        )
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS report_stats (
            source_pdf TEXT PRIMARY KEY,
            project TEXT,
            expected_files INTEGER,
            extracted_files INTEGER NOT NULL,
            difference INTEGER,
            sequence_files INTEGER NOT NULL,
            size_unknown_files INTEGER NOT NULL,
            issues INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS lto_entries (
            id INTEGER PRIMARY KEY,
            source_pdf TEXT NOT NULL,
            source_page INTEGER NOT NULL,
            project TEXT NOT NULL,
            tape TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            norm_path TEXT NOT NULL,
            top_folder TEXT NOT NULL,
            norm_top_folder TEXT NOT NULL,
            filename TEXT NOT NULL,
            norm_filename TEXT NOT NULL,
            reported_size TEXT NOT NULL,
            size_min_bytes INTEGER,
            size_max_bytes INTEGER,
            size_known INTEGER NOT NULL,
            entry_kind TEXT NOT NULL,
            sequence_id TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_lto_norm_path
            ON lto_entries(norm_path);
        CREATE INDEX IF NOT EXISTS idx_lto_move_candidate
            ON lto_entries(norm_top_folder, norm_filename);
        CREATE INDEX IF NOT EXISTS idx_lto_location
            ON lto_entries(tape, norm_path);

        CREATE TABLE IF NOT EXISTS parse_issues (
            id INTEGER PRIMARY KEY,
            source_pdf TEXT NOT NULL,
            page INTEGER NOT NULL,
            issue_type TEXT NOT NULL,
            details TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS storage_entries (
            id INTEGER PRIMARY KEY,
            source_root TEXT NOT NULL,
            absolute_path TEXT NOT NULL,
            absolute_path_bytes BLOB,
            relative_path TEXT NOT NULL,
            relative_path_bytes BLOB,
            path_encoding_valid INTEGER NOT NULL DEFAULT 1,
            norm_path TEXT NOT NULL,
            top_folder TEXT NOT NULL,
            norm_top_folder TEXT NOT NULL,
            filename TEXT NOT NULL,
            norm_filename TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_storage_norm_path
            ON storage_entries(norm_path);
        CREATE INDEX IF NOT EXISTS idx_storage_move_candidate
            ON storage_entries(norm_top_folder, norm_filename);

        CREATE TABLE IF NOT EXISTS scan_issues (
            id INTEGER PRIMARY KEY,
            source_root TEXT NOT NULL,
            path TEXT NOT NULL,
            issue_type TEXT NOT NULL,
            details TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS servers (
            id INTEGER PRIMARY KEY,
            hostname TEXT NOT NULL UNIQUE,
            ip_address TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS volumes (
            id INTEGER PRIMARY KEY,
            server_id INTEGER NOT NULL REFERENCES servers(id),
            name TEXT NOT NULL,
            root_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(server_id, name)
        );

        CREATE TABLE IF NOT EXISTS storage_scans (
            id INTEGER PRIMARY KEY,
            snapshot_id TEXT NOT NULL UNIQUE,
            server_id INTEGER NOT NULL REFERENCES servers(id),
            volume_id INTEGER NOT NULL REFERENCES volumes(id),
            source_root TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            file_count INTEGER NOT NULL,
            total_size_bytes INTEGER NOT NULL,
            issue_count INTEGER NOT NULL,
            snapshot_schema_version INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS storage_files (
            id INTEGER PRIMARY KEY,
            scan_id INTEGER NOT NULL REFERENCES storage_scans(id),
            server_id INTEGER NOT NULL REFERENCES servers(id),
            volume_id INTEGER NOT NULL REFERENCES volumes(id),
            absolute_path TEXT NOT NULL,
            absolute_path_bytes BLOB NOT NULL,
            relative_path TEXT NOT NULL,
            relative_path_bytes BLOB NOT NULL,
            path_encoding_valid INTEGER NOT NULL,
            project_folder TEXT NOT NULL,
            norm_project_folder TEXT NOT NULL,
            filename TEXT NOT NULL,
            norm_filename TEXT NOT NULL,
            norm_path TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            UNIQUE(scan_id, relative_path_bytes)
        );

        CREATE INDEX IF NOT EXISTS idx_storage_files_volume_norm_path
            ON storage_files(volume_id, norm_path);
        CREATE INDEX IF NOT EXISTS idx_storage_files_norm_filename_size
            ON storage_files(norm_filename, size_bytes);

        CREATE TABLE IF NOT EXISTS storage_scan_issues (
            id INTEGER PRIMARY KEY,
            scan_id INTEGER NOT NULL REFERENCES storage_scans(id),
            path TEXT NOT NULL,
            issue_type TEXT NOT NULL,
            details TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS archive_imports (
            id INTEGER PRIMARY KEY,
            source_kind TEXT NOT NULL,
            source_path TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            imported_at TEXT NOT NULL,
            row_count INTEGER NOT NULL,
            UNIQUE(source_kind, source_path)
        );

        CREATE TABLE IF NOT EXISTS archive_catalog_files (
            id INTEGER PRIMARY KEY,
            import_id INTEGER NOT NULL REFERENCES archive_imports(id),
            source_workbook TEXT NOT NULL,
            source_sheet TEXT NOT NULL,
            source_row INTEGER NOT NULL,
            project_raw TEXT NOT NULL,
            project_norm TEXT NOT NULL,
            cassette_raw TEXT NOT NULL,
            cassette_norm TEXT NOT NULL,
            path_raw TEXT NOT NULL,
            path_norm TEXT NOT NULL,
            filename_raw TEXT NOT NULL,
            filename_norm TEXT NOT NULL,
            size_raw TEXT NOT NULL,
            size_bytes INTEGER,
            UNIQUE(import_id, source_sheet, source_row)
        );

        CREATE INDEX IF NOT EXISTS idx_archive_catalog_filename
            ON archive_catalog_files(filename_norm);
        CREATE INDEX IF NOT EXISTS idx_archive_catalog_cassette
            ON archive_catalog_files(cassette_norm);

        CREATE TABLE IF NOT EXISTS archive_manual_map (
            id INTEGER PRIMARY KEY,
            import_id INTEGER NOT NULL REFERENCES archive_imports(id),
            source_workbook TEXT NOT NULL,
            source_sheet TEXT NOT NULL,
            source_row INTEGER NOT NULL,
            source_column TEXT NOT NULL,
            project_raw TEXT NOT NULL,
            project_norm TEXT NOT NULL,
            cassette_raw TEXT NOT NULL,
            cassette_norm TEXT NOT NULL,
            cassette_note_raw TEXT NOT NULL,
            folder_path_raw TEXT NOT NULL,
            folder_path_norm TEXT NOT NULL,
            UNIQUE(import_id, source_sheet, source_row, source_column)
        );

        CREATE INDEX IF NOT EXISTS idx_archive_manual_cassette
            ON archive_manual_map(cassette_norm);
        """
    )
    # Upgrade databases created by v1.0.0 in place. The scan stage replaces all
    # storage rows, so nullable BLOB columns are safe for the migration itself.
    existing_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(storage_entries)")
    }
    if "absolute_path_bytes" not in existing_columns:
        connection.execute("ALTER TABLE storage_entries ADD COLUMN absolute_path_bytes BLOB")
    if "relative_path_bytes" not in existing_columns:
        connection.execute("ALTER TABLE storage_entries ADD COLUMN relative_path_bytes BLOB")
    if "path_encoding_valid" not in existing_columns:
        connection.execute(
            "ALTER TABLE storage_entries ADD COLUMN path_encoding_valid INTEGER NOT NULL DEFAULT 1"
        )
    connection.execute(f"PRAGMA user_version={CENTRAL_SCHEMA_VERSION}")
    connection.commit()


def insert_lto_data(
    connection: sqlite3.Connection,
    entries: Sequence[LtoEntry],
    issues: Sequence[dict],
    stats: dict,
) -> None:
    connection.executemany(
        """
        INSERT INTO lto_entries (
            source_pdf, source_page, project, tape, relative_path, norm_path,
            top_folder, norm_top_folder, filename, norm_filename,
            reported_size, size_min_bytes, size_max_bytes, size_known,
            entry_kind, sequence_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                entry.source_pdf,
                entry.source_page,
                entry.project,
                entry.tape,
                entry.relative_path,
                entry.norm_path,
                entry.top_folder,
                entry.norm_top_folder,
                entry.filename,
                entry.norm_filename,
                entry.reported_size,
                entry.size_min_bytes,
                entry.size_max_bytes,
                entry.size_known,
                entry.entry_kind,
                entry.sequence_id,
            )
            for entry in entries
        ],
    )
    connection.executemany(
        """
        INSERT INTO parse_issues (source_pdf, page, issue_type, details)
        VALUES (?, ?, ?, ?)
        """,
        [
            (
                issue["source_pdf"],
                int(issue["page"]),
                issue["issue_type"],
                issue["details"],
            )
            for issue in issues
        ],
    )
    connection.execute(
        """
        INSERT OR REPLACE INTO report_stats (
            source_pdf, project, expected_files, extracted_files, difference,
            sequence_files, size_unknown_files, issues
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            stats["source_pdf"],
            stats["project"],
            stats["expected_files"],
            stats["extracted_files"],
            stats["difference"],
            stats["sequence_files"],
            stats["size_unknown_files"],
            stats["issues"],
        ),
    )
    connection.commit()


def command_extract(args: argparse.Namespace) -> int:
    db_path = Path(args.db).expanduser().resolve()
    pdf_paths = [Path(item).expanduser().resolve() for item in args.pdf]
    pdf_size_units = getattr(args, "pdf_size_units", "decimal")
    if pdf_size_units not in PDF_SIZE_UNIT_TABLES:
        raise ValueError(f"Unsupported PDF size unit mode: {pdf_size_units!r}")
    for pdf_path in pdf_paths:
        if not pdf_path.is_file():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

    with connect_db(db_path) as connection:
        existing_lto_count = connection.execute(
            "SELECT COUNT(*) FROM lto_entries"
        ).fetchone()[0]
        if args.append and existing_lto_count:
            stored_units = read_pdf_size_unit_info(connection)
            if stored_units.mode != pdf_size_units:
                raise RuntimeError(
                    "Cannot append LTO records using PDF size units "
                    f"{pdf_size_units!r}; existing intervals use "
                    f"{stored_units.label!r}. Re-extract all PDFs into a clean "
                    "LTO manifest instead of mixing unit interpretations."
                )
        if not args.append:
            connection.executescript(
                "DELETE FROM lto_entries; DELETE FROM parse_issues; DELETE FROM report_stats;"
            )
            connection.commit()
        # Persist the interpretation before inserting any committed PDF rows so
        # an interrupted multi-PDF extraction can never leave unlabelled ranges.
        set_metadata(connection, "pdf_size_units", pdf_size_units)

        for pdf_path in pdf_paths:
            eprint(f"[extract] {pdf_path}")
            entries, issues, stats = parse_yoyotta_pdf(
                pdf_path, pdf_size_units=pdf_size_units
            )
            insert_lto_data(connection, entries, issues, stats)
            eprint(
                f"[extract] project={stats['project']!r}, "
                f"files={stats['extracted_files']}, expected={stats['expected_files']}, "
                f"issues={stats['issues']}"
            )

        set_metadata(connection, "lto_extracted_at", dt.datetime.now().isoformat())
        set_metadata(connection, "tool_version", VERSION)
    eprint(f"[extract] database: {db_path}")
    return 0


def set_metadata(connection: sqlite3.Connection, key: str, value: object) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
        (key, json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value),
    )
    connection.commit()


def read_pdf_size_unit_info(connection: sqlite3.Connection) -> PdfSizeUnitInfo:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key = 'pdf_size_units'"
    ).fetchone()
    if row is None:
        return PdfSizeUnitInfo(
            mode="binary",
            label="LEGACY_BINARY_ASSUMED",
            warning=(
                "Database has no pdf_size_units metadata. Stored intervals are "
                "treated as legacy binary interpretation; re-extract the PDFs "
                "with --pdf-size-units decimal if YoYotta used decimal units."
            ),
        )
    value = str(row[0]).strip().lower()
    if value in PDF_SIZE_UNIT_TABLES:
        return PdfSizeUnitInfo(mode=value, label=value)
    return PdfSizeUnitInfo(
        mode="binary",
        label=f"UNKNOWN_METADATA({value or 'empty'})_BINARY_ASSUMED",
        warning=(
            f"Database contains unsupported pdf_size_units={value!r}. Stored "
            "intervals are used unchanged and reported as legacy binary-assumed."
        ),
    )


def ensure_not_nested(output_path: Path, roots: Sequence[Path]) -> None:
    output_real = Path(os.path.realpath(str(output_path)))
    for root in roots:
        root_real = Path(os.path.realpath(str(root)))
        try:
            common = Path(os.path.commonpath([str(output_real), str(root_real)]))
        except ValueError:
            continue
        if common == root_real:
            raise RuntimeError(
                f"Output path must not be inside a source tree: {output_path} is under {root}"
            )


def stored_source_roots(connection: sqlite3.Connection) -> List[Path]:
    """Return all source roots recorded in rows or scan metadata."""
    roots = {
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT source_root FROM storage_entries WHERE source_root <> ''"
        )
    }
    metadata_row = connection.execute(
        "SELECT value FROM metadata WHERE key = 'storage_roots'"
    ).fetchone()
    if metadata_row:
        try:
            metadata_roots = json.loads(metadata_row[0])
            if isinstance(metadata_roots, list):
                roots.update(str(item) for item in metadata_roots if isinstance(item, str))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return [Path(root).expanduser().resolve() for root in sorted(roots)]


def ensure_report_output_outside_stored_roots(
    connection: sqlite3.Connection, output_path: Path
) -> None:
    roots = stored_source_roots(connection)
    if not roots:
        raise RuntimeError(
            "Database contains no recorded storage roots; cannot verify report output safety."
        )
    ensure_not_nested(output_path, roots)


DISCOVERY_FIELDNAMES = [
    "lto_top_folder",
    "normalized_lto_top_folder",
    "status",
    "raid_root",
    "source_root",
    "project_name",
    "source_directory",
    "physical_folder",
    "candidate_count",
    "anchors_tested",
    "anchors_path_matched",
    "anchors_size_matched",
    "anchors_size_mismatched",
    "unknown_size_paths_matched",
    "confidence_reason",
]


def discover_source_directories(
    raid_roots: Sequence[Path], *, max_depth: int
) -> Tuple[List[SourceDirectory], List[str]]:
    """Find SOURCE directories without following links or reading file data."""
    if max_depth < 1:
        raise ValueError("--max-source-depth must be at least 1")
    found: List[SourceDirectory] = []
    issues: List[str] = []
    for raid_root in raid_roots:
        stack: List[Tuple[Path, int]] = [(raid_root, 0)]
        while stack:
            directory, depth = stack.pop()
            if depth >= max_depth:
                continue
            try:
                with os.scandir(directory) as scan:
                    children = list(scan)
            except OSError as exc:
                issues.append(f"directory_read_error:{safe_filesystem_display(str(directory))}:{exc!r}")
                continue
            children.sort(key=lambda item: (normalize_component(item.name), item.name))
            for child in children:
                if child.name.startswith("."):
                    continue
                try:
                    if child.is_symlink() or not child.is_dir(follow_symlinks=False):
                        continue
                except OSError as exc:
                    issues.append(
                        f"directory_stat_error:{safe_filesystem_display(child.path)}:{exc!r}"
                    )
                    continue
                child_path = Path(child.path)
                child_depth = depth + 1
                if normalize_component(child.name) == normalize_component("SOURCE"):
                    found.append(
                        SourceDirectory(
                            raid_root=raid_root,
                            project_name=safe_filesystem_display(directory.name),
                            path=child_path,
                        )
                    )
                    continue
                if child_depth < max_depth:
                    stack.append((child_path, child_depth))
    found.sort(
        key=lambda item: (
            normalize_component(str(item.raid_root)),
            normalize_component(str(item.path)),
            str(item.path),
        )
    )
    return found, issues


def source_top_folders(
    sources: Sequence[SourceDirectory], issues: List[str]
) -> List[DiscoveryCandidate]:
    candidates: List[DiscoveryCandidate] = []
    for source in sources:
        try:
            with os.scandir(source.path) as scan:
                children = list(scan)
        except OSError as exc:
            issues.append(
                f"directory_read_error:{safe_filesystem_display(str(source.path))}:{exc!r}"
            )
            continue
        children.sort(key=lambda item: (normalize_component(item.name), item.name))
        for child in children:
            if child.name.startswith("."):
                continue
            try:
                if child.is_symlink() or not child.is_dir(follow_symlinks=False):
                    continue
            except OSError as exc:
                issues.append(
                    f"directory_stat_error:{safe_filesystem_display(child.path)}:{exc!r}"
                )
                continue
            candidates.append(
                DiscoveryCandidate(source=source, physical_folder=Path(child.path))
            )
    return candidates


def select_discovery_anchors(
    entries: Sequence[LtoEntry],
    *,
    filename_path_counts: Dict[str, int],
    below_top_counts: Dict[str, int],
    maximum: int,
) -> List[DiscoveryAnchor]:
    """Choose deterministic, path-based anchors; repeated tape copies count once."""
    grouped: Dict[str, List[LtoEntry]] = defaultdict(list)
    for entry in entries:
        grouped[entry.norm_path].append(entry)
    known: List[Tuple[Tuple[object, ...], DiscoveryAnchor]] = []
    unknown: List[Tuple[Tuple[object, ...], DiscoveryAnchor]] = []
    for norm_path, path_entries in grouped.items():
        representative = sorted(
            path_entries,
            key=lambda item: (item.relative_path, item.tape, item.source_pdf, item.source_page),
        )[0]
        parts = PurePosixPath(representative.relative_path).parts
        if len(parts) < 2:
            continue
        below = PurePosixPath(*parts[1:]).as_posix()
        norm_below = normalize_relative_path(below)
        known_ranges = {
            (entry.size_min_bytes, entry.size_max_bytes)
            for entry in path_entries
            if entry.size_known
        }
        contains_unknown = any(not entry.size_known for entry in path_entries)
        size_known = len(known_ranges) == 1 and not contains_unknown
        minimum: Optional[int] = None
        maximum_bytes: Optional[int] = None
        if size_known:
            minimum, maximum_bytes = next(iter(known_ranges))
        anchor = DiscoveryAnchor(
            relative_below_top=below,
            norm_path=norm_path,
            filename=representative.filename,
            size_min_bytes=minimum,
            size_max_bytes=maximum_bytes,
            size_known=size_known,
            distinctive_below_top=below_top_counts.get(norm_below, 0) == 1,
        )
        parent = normalize_relative_path(str(PurePosixPath(below).parent))
        key: Tuple[object, ...] = (
            filename_path_counts.get(representative.norm_filename, 0),
            parent,
            norm_path,
        )
        (known if size_known else unknown).append((key, anchor))

    known.sort(key=lambda item: item[0])
    unknown.sort(key=lambda item: item[0])
    selected: List[DiscoveryAnchor] = []
    used_parents = set()
    for _, anchor in known:
        parent = normalize_relative_path(str(PurePosixPath(anchor.relative_below_top).parent))
        if parent not in used_parents:
            selected.append(anchor)
            used_parents.add(parent)
            if len(selected) >= maximum:
                return selected
    for _, anchor in known:
        if anchor not in selected:
            selected.append(anchor)
            if len(selected) >= maximum:
                return selected
    for _, anchor in unknown:
        selected.append(anchor)
        if len(selected) >= maximum:
            break
    return selected


def normalized_descendant_file(
    base: Path, relative_path: str
) -> Tuple[Optional[Path], Optional[int], str]:
    """Resolve every path component case-insensitively without following links."""
    current = base
    parts = PurePosixPath(relative_path).parts
    for index, expected in enumerate(parts):
        try:
            with os.scandir(current) as scan:
                matches = [
                    item
                    for item in scan
                    if normalize_component(item.name) == normalize_component(expected)
                ]
        except OSError as exc:
            return None, None, f"directory_read_error:{safe_filesystem_display(str(current))}:{exc!r}"
        matches.sort(key=lambda item: item.name)
        usable = []
        for item in matches:
            try:
                if item.is_symlink():
                    continue
                if index < len(parts) - 1:
                    if item.is_dir(follow_symlinks=False):
                        usable.append(item)
                elif item.is_file(follow_symlinks=False):
                    usable.append(item)
            except OSError:
                continue
        if len(usable) != 1:
            reason = "component_not_found" if not usable else "normalized_component_ambiguous"
            return None, None, reason
        current = Path(usable[0].path)
    try:
        stat_result = os.stat(current, follow_symlinks=False)
    except OSError as exc:
        return None, None, f"file_stat_error:{safe_filesystem_display(str(current))}:{exc!r}"
    return current, stat_result.st_size, "found"


def score_discovery_candidate(
    candidate: DiscoveryCandidate,
    anchors: Sequence[DiscoveryAnchor],
    issues: List[str],
) -> DiscoveryCandidateScore:
    path_matches = 0
    size_matches_count = 0
    size_mismatches = 0
    unknown_matches = 0
    for anchor in anchors:
        _, size, outcome = normalized_descendant_file(
            candidate.physical_folder, anchor.relative_below_top
        )
        if size is None:
            if outcome.startswith(("directory_read_error:", "file_stat_error:")):
                issues.append(
                    f"anchor_{outcome};candidate="
                    f"{safe_filesystem_display(str(candidate.physical_folder))}"
                )
            continue
        path_matches += 1
        if anchor.size_known:
            assert anchor.size_min_bytes is not None
            assert anchor.size_max_bytes is not None
            if anchor.size_min_bytes <= size <= anchor.size_max_bytes:
                size_matches_count += 1
            else:
                size_mismatches += 1
        else:
            unknown_matches += 1
    return DiscoveryCandidateScore(
        candidate=candidate,
        anchors_tested=len(anchors),
        anchors_path_matched=path_matches,
        anchors_size_matched=size_matches_count,
        anchors_size_mismatched=size_mismatches,
        unknown_size_paths_matched=unknown_matches,
    )


def discovery_row(
    *,
    top_folder: str,
    norm_top_folder: str,
    status: str,
    scores: Sequence[DiscoveryCandidateScore],
    anchors_tested: int,
    reason: str,
    chosen_score: Optional[DiscoveryCandidateScore] = None,
) -> dict:
    ranked = sorted(
        scores,
        key=lambda score: (
            -score.anchors_size_matched,
            -score.anchors_path_matched,
            score.anchors_size_mismatched,
            str(score.candidate.physical_folder),
        ),
    )
    chosen = chosen_score or (ranked[0] if len(ranked) == 1 else None)
    candidate = chosen.candidate if chosen else None
    metric_score = chosen or (ranked[0] if ranked else None)
    return {
        "lto_top_folder": top_folder,
        "normalized_lto_top_folder": norm_top_folder,
        "status": status,
        "raid_root": (
            safe_filesystem_display(str(candidate.source.raid_root)) if candidate else ""
        ),
        "source_root": (
            safe_filesystem_display(str(candidate.source.path)) if candidate else ""
        ),
        "project_name": candidate.source.project_name if candidate else "",
        "source_directory": (
            safe_filesystem_display(str(candidate.source.path)) if candidate else ""
        ),
        "physical_folder": (
            safe_filesystem_display(str(candidate.physical_folder)) if candidate else ""
        ),
        "candidate_count": len(scores),
        "anchors_tested": anchors_tested,
        "anchors_path_matched": (
            metric_score.anchors_path_matched if metric_score else ""
        ),
        "anchors_size_matched": (
            metric_score.anchors_size_matched if metric_score else ""
        ),
        "anchors_size_mismatched": (
            metric_score.anchors_size_mismatched if metric_score else ""
        ),
        "unknown_size_paths_matched": (
            metric_score.unknown_size_paths_matched if metric_score else ""
        ),
        "confidence_reason": reason,
    }


def discover_lto_mappings(
    lto_entries: Sequence[LtoEntry],
    sources: Sequence[SourceDirectory],
    *,
    maximum_anchors: int,
    minimum_anchor_matches: int,
    issues: List[str],
) -> List[dict]:
    if maximum_anchors < 1:
        raise ValueError("--anchors-per-folder must be at least 1")
    if minimum_anchor_matches < 1:
        raise ValueError("--min-anchor-matches must be at least 1")
    by_top: Dict[str, List[LtoEntry]] = defaultdict(list)
    filename_paths: Dict[str, set[str]] = defaultdict(set)
    below_top_folders: Dict[str, set[str]] = defaultdict(set)
    for entry in lto_entries:
        by_top[entry.norm_top_folder].append(entry)
        filename_paths[entry.norm_filename].add(entry.norm_path)
        parts = PurePosixPath(entry.relative_path).parts
        if len(parts) >= 2:
            below_top_folders[
                normalize_relative_path(PurePosixPath(*parts[1:]).as_posix())
            ].add(entry.norm_top_folder)
    filename_path_counts = {key: len(value) for key, value in filename_paths.items()}
    below_top_counts = {key: len(value) for key, value in below_top_folders.items()}
    all_candidates = source_top_folders(sources, issues)
    rows: List[dict] = []
    for norm_top in sorted(by_top):
        entries = by_top[norm_top]
        top_folder = sorted(
            {entry.top_folder for entry in entries},
            key=lambda value: (normalize_component(value), value),
        )[0]
        anchors = select_discovery_anchors(
            entries,
            filename_path_counts=filename_path_counts,
            below_top_counts=below_top_counts,
            maximum=maximum_anchors,
        )
        name_candidates = [
            candidate
            for candidate in all_candidates
            if normalize_component(candidate.physical_folder.name) == norm_top
        ]
        fallback = not name_candidates
        if fallback:
            anchors = [anchor for anchor in anchors if anchor.distinctive_below_top]
        evaluated = name_candidates if name_candidates else all_candidates
        if fallback:
            eprint(f"[discover] fallback anchors: {top_folder}")
        scores = [
            score_discovery_candidate(candidate, anchors, issues)
            for candidate in evaluated
        ]
        if fallback:
            scores = [score for score in scores if score.anchors_path_matched > 0]
        strong = [
            score
            for score in scores
            if score.anchors_size_matched >= minimum_anchor_matches
            and score.anchors_size_mismatched == 0
            and not has_surrogateescape(str(score.candidate.source.path))
            and not has_surrogateescape(str(score.candidate.physical_folder))
        ]
        plausible = [
            score
            for score in scores
            if score.anchors_path_matched > 0
            and score.anchors_size_mismatched == 0
        ]
        if len(strong) == 1:
            status = "MATCHED"
            reason = (
                "UNIQUE_STRONG_PATH_AND_SIZE_ANCHORS"
                f";matched={strong[0].anchors_size_matched}"
                f";fallback={'yes' if fallback else 'no'}"
            )
        elif len(strong) > 1 or len(plausible) > 1:
            status = "AMBIGUOUS"
            ambiguous_scores = strong if strong else plausible
            reason = (
                "MULTIPLE_PLAUSIBLE_CANDIDATES"
                f";strong={len(strong)};plausible={len(plausible)}"
                ";candidates="
                + "|".join(
                    safe_filesystem_display(str(score.candidate.physical_folder))
                    for score in sorted(
                        ambiguous_scores,
                        key=lambda item: str(item.candidate.physical_folder),
                    )
                )
            )
        elif len(plausible) == 1:
            status = "NAME_MATCH_UNVERIFIED"
            reason = (
                "INSUFFICIENT_KNOWN_SIZE_ANCHORS"
                f";required={minimum_anchor_matches}"
                f";matched={plausible[0].anchors_size_matched}"
            )
        elif name_candidates:
            status = "NAME_MATCH_UNVERIFIED"
            reason = "NAME_MATCH_WITHOUT_USABLE_ANCHOR_EVIDENCE"
        elif scores:
            status = "NAME_MATCH_UNVERIFIED"
            reason = "FALLBACK_PATH_HIT_WITHOUT_USABLE_SIZE_EVIDENCE"
        else:
            status = "NOT_FOUND"
            reason = "NO_NAME_OR_PATH_ANCHOR_CANDIDATE"
        rows.append(
            discovery_row(
                top_folder=top_folder,
                norm_top_folder=norm_top,
                status=status,
                scores=scores,
                anchors_tested=len(anchors),
                reason=reason,
                chosen_score=strong[0] if status == "MATCHED" else None,
            )
        )
    return rows


def mount_options_for(path: Path) -> Optional[List[str]]:
    findmnt = shutil.which("findmnt")
    if not findmnt:
        return None
    proc = subprocess.run(
        [findmnt, "-T", str(path), "-n", "-o", "OPTIONS"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        return None
    options = proc.stdout.strip()
    return [item.strip() for item in options.split(",") if item.strip()]


def mount_filesystem_type_for(path: Path) -> Optional[str]:
    findmnt = shutil.which("findmnt")
    if not findmnt:
        return None
    proc = subprocess.run(
        [findmnt, "-T", str(path), "-n", "-o", "FSTYPE"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip().lower() or None


def require_local_storage_root(root: Path) -> None:
    filesystem_type = mount_filesystem_type_for(root)
    if filesystem_type is None:
        raise RuntimeError(
            f"Cannot verify filesystem type for {root}; local scan-storage "
            "must not crawl SMB/NFS storage."
        )
    remote_types = {"cifs", "smb3", "nfs", "nfs4", "sshfs", "fuse.sshfs"}
    if filesystem_type in remote_types:
        raise RuntimeError(
            f"scan-storage requires a local volume, not {filesystem_type}: {root}"
        )


def require_read_only_roots(
    roots: Sequence[Path], allow_rw: bool, *, operation: str = "scan"
) -> None:
    for root in roots:
        options = mount_options_for(root)
        if options is None:
            message = (
                f"Cannot verify mount options for {root}; findmnt is unavailable or failed."
            )
            if allow_rw:
                eprint(f"[{operation}] WARNING: {message}")
                continue
            raise RuntimeError(message + " Use a verified RO bind mount or --allow-rw-source.")
        if "ro" not in options:
            if allow_rw:
                eprint(
                    f"[{operation}] WARNING: source appears writable: "
                    f"{root} ({','.join(options)})"
                )
            else:
                raise RuntimeError(
                    f"Source is not mounted read-only: {root} ({','.join(options)}). "
                    "Use an RO bind mount. Override only deliberately with --allow-rw-source."
                )
        else:
            eprint(f"[{operation}] verified read-only mount: {root}")


def write_discovery_reports(rows: Sequence[dict], out_dir: Path) -> None:
    selections = (
        ("discovery_mapping.csv", rows),
        (
            "discovery_ambiguous.csv",
            [
                row
                for row in rows
                if row["status"] in ("AMBIGUOUS", "NAME_MATCH_UNVERIFIED")
            ],
        ),
        (
            "discovery_not_found.csv",
            [row for row in rows if row["status"] == "NOT_FOUND"],
        ),
    )
    for filename, selected in selections:
        with (out_dir / filename).open(
            "w", encoding="utf-8-sig", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=DISCOVERY_FIELDNAMES)
            writer.writeheader()
            writer.writerows(selected)


def print_discovery_summary(rows: Sequence[dict]) -> None:
    headings = ("LTO folder", "Project", "Physical folder", "Status")
    table_rows = []
    for row in rows:
        table_rows.append(
            (
                str(row["lto_top_folder"]),
                str(row["project_name"] or "?"),
                str(row["physical_folder"] or "-"),
                str(row["status"]),
            )
        )
    widths = [
        max([len(headings[index])] + [len(row[index]) for row in table_rows])
        for index in range(len(headings))
    ]

    def line(values: Sequence[str]) -> str:
        return "  ".join(
            value.ljust(widths[index]) for index, value in enumerate(values)
        ).rstrip()

    print(line(headings))
    print(line(tuple("-" * width for width in widths)))
    for row in table_rows:
        print(line(row))
    counts = {
        status: sum(row["status"] == status for row in rows)
        for status in ("MATCHED", "AMBIGUOUS", "NOT_FOUND")
    }
    counts["UNVERIFIED"] = sum(
        row["status"] == "NAME_MATCH_UNVERIFIED" for row in rows
    )
    print("")
    for status in ("MATCHED", "AMBIGUOUS", "NOT_FOUND", "UNVERIFIED"):
        print(f"{status}: {counts[status]}")


def command_discover(args: argparse.Namespace) -> int:
    db_path = Path(args.db).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    raid_roots = sorted(
        {Path(item).expanduser().resolve() for item in args.raid_root},
        key=lambda path: (normalize_component(str(path)), str(path)),
    )
    for raid_root in raid_roots:
        if not raid_root.is_dir():
            raise NotADirectoryError(f"RAID root not found: {raid_root}")
    ensure_not_nested(out_dir, raid_roots)
    require_read_only_roots(
        raid_roots, args.allow_rw_source, operation="discover"
    )
    with connect_db_readonly(db_path) as connection:
        lto_entries = [
            row_to_lto(row)
            for row in connection.execute(
                "SELECT * FROM lto_entries ORDER BY norm_path, id"
            )
        ]
    if not lto_entries:
        raise RuntimeError("Database has no LTO entries. Run extract first.")
    sources, issues = discover_source_directories(
        raid_roots, max_depth=args.max_source_depth
    )
    if not sources:
        raise RuntimeError(
            "No SOURCE directories found under the supplied RAID roots within "
            f"depth {args.max_source_depth}."
        )
    eprint(f"[discover] SOURCE directories={len(sources):,}")
    rows = discover_lto_mappings(
        lto_entries,
        sources,
        maximum_anchors=args.anchors_per_folder,
        minimum_anchor_matches=args.min_anchor_matches,
        issues=issues,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    write_discovery_reports(rows, out_dir)
    for issue in issues:
        eprint(f"[discover] WARNING: {issue}")
    print_discovery_summary(rows)
    eprint(f"[discover] reports={out_dir}")
    return 0


def load_matched_scan_roots(mapping_path: Path) -> List[MappedScanRoot]:
    mapping_path = mapping_path.expanduser().resolve()
    if not mapping_path.is_file():
        raise FileNotFoundError(f"Discovery mapping not found: {mapping_path}")
    with mapping_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "lto_top_folder",
            "normalized_lto_top_folder",
            "status",
            "source_directory",
            "physical_folder",
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise RuntimeError(
                "Discovery mapping is missing columns: " + ", ".join(sorted(missing))
            )
        rows = list(reader)
    jobs: List[MappedScanRoot] = []
    physical_seen = set()
    logical_seen = set()
    for row in rows:
        if row["status"] != "MATCHED":
            continue
        logical_top = row["lto_top_folder"]
        if not logical_top or "/" in logical_top or "\\" in logical_top:
            raise RuntimeError(f"Invalid mapped LTO top folder: {logical_top!r}")
        if normalize_component(logical_top) != row["normalized_lto_top_folder"]:
            raise RuntimeError(f"Mapping normalization mismatch for {logical_top!r}")
        source_unresolved = Path(row["source_directory"]).expanduser()
        if not source_unresolved.is_absolute():
            raise RuntimeError(
                f"Mapped SOURCE directory must be absolute: {source_unresolved}"
            )
        if source_unresolved.is_symlink():
            raise RuntimeError(
                f"Mapped SOURCE directory must not be a symlink: {source_unresolved}"
            )
        source = source_unresolved.resolve()
        physical_text = row["physical_folder"]
        physical_unresolved = Path(physical_text).expanduser()
        if not physical_unresolved.is_absolute():
            raise RuntimeError(
                f"Mapped physical folder must be absolute: {physical_text}"
            )
        if physical_unresolved.is_symlink():
            raise RuntimeError(f"Mapped physical folder must not be a symlink: {physical_text}")
        physical = physical_unresolved.resolve()
        if not source.is_dir() or not physical.is_dir():
            raise NotADirectoryError(
                f"Mapped SOURCE or physical folder is unavailable: {source}, {physical}"
            )
        if physical.parent != source:
            raise RuntimeError(
                f"Mapped physical folder must be directly below SOURCE: {physical}"
            )
        physical_key = str(physical)
        logical_key = normalize_component(logical_top)
        if physical_key in physical_seen:
            raise RuntimeError(f"Physical folder mapped more than once: {physical}")
        if logical_key in logical_seen:
            raise RuntimeError(f"LTO top folder mapped more than once: {logical_top}")
        physical_seen.add(physical_key)
        logical_seen.add(logical_key)
        jobs.append(
            MappedScanRoot(
                source_directory=source,
                physical_folder=physical,
                lto_top_folder=logical_top,
            )
        )
    if not jobs:
        raise RuntimeError("Discovery mapping contains no MATCHED folders.")
    return sorted(
        jobs,
        key=lambda job: (
            normalize_component(str(job.source_directory)),
            normalize_component(job.lto_top_folder),
            str(job.physical_folder),
        ),
    )


def scan_root(
    root: Path,
    *,
    progress_every: int = 10000,
    quiet: bool = False,
    walk_root: Optional[Path] = None,
    logical_top_folder: Optional[str] = None,
) -> Tuple[Iterator[StorageEntry], dict, List[dict]]:
    """Iterative scandir walk; never follows symlinks."""
    scan_start = walk_root or root
    if logical_top_folder is not None and walk_root is None:
        raise ValueError("logical_top_folder requires walk_root")
    stats = {
        "source_root": str(root),
        "files": 0,
        "bytes": 0,
        "hidden_files_skipped": 0,
        "hidden_dirs_skipped": 0,
        "symlinks_skipped": 0,
        "special_files_skipped": 0,
        "non_utf8_paths": 0,
        "errors": 0,
    }
    issues: List[dict] = []

    def iterator() -> Iterator[StorageEntry]:
        stack: List[Path] = [scan_start]
        while stack:
            directory = stack.pop()
            try:
                with os.scandir(directory) as scan:
                    children = list(scan)
            except OSError as exc:
                stats["errors"] += 1
                issues.append(
                    {
                        "source_root": str(root),
                        "path": safe_filesystem_display(str(directory)),
                        "issue_type": "directory_read_error",
                        "details": repr(exc),
                    }
                )
                continue

            # Sorting makes reports reproducible; reverse for LIFO directory order.
            children.sort(key=lambda item: normalize_component(item.name), reverse=True)
            for child in children:
                child_path = Path(child.path)
                if child.name.startswith("."):
                    try:
                        if child.is_dir(follow_symlinks=False):
                            stats["hidden_dirs_skipped"] += 1
                        else:
                            stats["hidden_files_skipped"] += 1
                    except OSError:
                        stats["hidden_files_skipped"] += 1
                    continue

                try:
                    if child.is_symlink():
                        stats["symlinks_skipped"] += 1
                        continue
                    if child.is_dir(follow_symlinks=False):
                        stack.append(child_path)
                        continue
                    if not child.is_file(follow_symlinks=False):
                        stats["special_files_skipped"] += 1
                        continue
                    stat_result = child.stat(follow_symlinks=False)
                except OSError as exc:
                    stats["errors"] += 1
                    issues.append(
                        {
                            "source_root": str(root),
                            "path": safe_filesystem_display(str(child_path)),
                            "issue_type": "file_stat_error",
                            "details": repr(exc),
                        }
                    )
                    continue

                raw_absolute_text = str(child_path)
                physical_relative_text = child_path.relative_to(root).as_posix()
                if logical_top_folder is None:
                    comparison_relative_text = physical_relative_text
                else:
                    below_mapped_folder = child_path.relative_to(scan_start).as_posix()
                    comparison_relative_text = PurePosixPath(
                        logical_top_folder, below_mapped_folder
                    ).as_posix()
                absolute_path_bytes = os.fsencode(raw_absolute_text)
                # Keep raw physical bytes for folder identity and NUL output,
                # while comparison_relative_text carries the mapped LTO identity.
                relative_path_bytes = os.fsencode(physical_relative_text)
                encoding_valid = not (
                    has_surrogateescape(raw_absolute_text)
                    or has_surrogateescape(physical_relative_text)
                )
                absolute_display = safe_filesystem_display(raw_absolute_text)
                relative_display = safe_filesystem_display(comparison_relative_text)
                top, filename = path_parts(relative_display)
                if encoding_valid:
                    norm_path = normalize_relative_path(relative_display)
                    norm_top_folder = normalize_component(top)
                    norm_filename = normalize_component(filename)
                else:
                    stats["non_utf8_paths"] += 1
                    norm_path = raw_norm_key(relative_path_bytes)
                    raw_parts = relative_path_bytes.split(b"/")
                    norm_top_folder = "__NON_UTF8_TOP__/" + (raw_parts[0].hex() if raw_parts else "")
                    norm_filename = "__NON_UTF8_NAME__/" + (raw_parts[-1].hex() if raw_parts else "")
                    issues.append(
                        {
                            "source_root": str(root),
                            "path": absolute_display,
                            "issue_type": "non_utf8_filesystem_name",
                            "details": (
                                "Filename contains raw bytes that are not valid UTF-8. "
                                "It is excluded from automatic matching/copy decisions; "
                                f"relative_path_bytes_hex={relative_path_bytes.hex()}"
                            ),
                        }
                    )
                entry = StorageEntry(
                    source_root=str(root),
                    absolute_path=absolute_display,
                    absolute_path_bytes=absolute_path_bytes,
                    relative_path=relative_display,
                    relative_path_bytes=relative_path_bytes,
                    path_encoding_valid=1 if encoding_valid else 0,
                    norm_path=norm_path,
                    top_folder=top,
                    norm_top_folder=norm_top_folder,
                    filename=filename,
                    norm_filename=norm_filename,
                    size_bytes=stat_result.st_size,
                    mtime_ns=getattr(stat_result, "st_mtime_ns", int(stat_result.st_mtime * 1e9)),
                )
                stats["files"] += 1
                stats["bytes"] += stat_result.st_size
                if not quiet and progress_every and stats["files"] % progress_every == 0:
                    eprint(
                        f"[scan] {root}: {stats['files']:,} files, "
                        f"{human_bytes(stats['bytes'])}"
                    )
                yield entry

    return iterator(), stats, issues


def insert_storage_batch(connection: sqlite3.Connection, batch: Sequence[StorageEntry]) -> None:
    connection.executemany(
        """
        INSERT INTO storage_entries (
            source_root, absolute_path, absolute_path_bytes,
            relative_path, relative_path_bytes, path_encoding_valid, norm_path,
            top_folder, norm_top_folder, filename, norm_filename,
            size_bytes, mtime_ns
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                entry.source_root,
                entry.absolute_path,
                sqlite3.Binary(entry.absolute_path_bytes),
                entry.relative_path,
                sqlite3.Binary(entry.relative_path_bytes),
                entry.path_encoding_valid,
                entry.norm_path,
                entry.top_folder,
                entry.norm_top_folder,
                entry.filename,
                entry.norm_filename,
                entry.size_bytes,
                entry.mtime_ns,
            )
            for entry in batch
        ],
    )


def command_scan(args: argparse.Namespace) -> int:
    db_path = Path(args.db).expanduser().resolve()
    mapping_value = getattr(args, "mapping", None)
    if mapping_value:
        mapped = load_matched_scan_roots(Path(mapping_value))
        scan_jobs: List[Tuple[Path, Optional[Path], Optional[str]]] = [
            (job.source_directory, job.physical_folder, job.lto_top_folder)
            for job in mapped
        ]
        roots = sorted(
            {job.source_directory for job in mapped},
            key=lambda path: (normalize_component(str(path)), str(path)),
        )
    else:
        root_values = getattr(args, "root", None)
        if not root_values:
            raise RuntimeError("scan requires --root or --mapping")
        roots = [Path(item).expanduser().resolve() for item in root_values]
        scan_jobs = [(root, None, None) for root in roots]
    for root in roots:
        if not root.is_dir():
            raise NotADirectoryError(f"Source root not found: {root}")

    ensure_not_nested(db_path, roots)
    require_read_only_roots(roots, args.allow_rw_source)

    with connect_db(db_path) as connection:
        connection.executescript("DELETE FROM storage_entries; DELETE FROM scan_issues;")
        connection.commit()
        stats_by_root: Dict[str, dict] = {}
        for root, walk_root, logical_top_folder in scan_jobs:
            label = str(walk_root or root)
            if logical_top_folder is not None:
                label += f" -> LTO/{logical_top_folder}"
            eprint(f"[scan] {label}")
            entries, stats, issues = scan_root(
                root,
                progress_every=args.progress_every,
                quiet=args.quiet,
                walk_root=walk_root,
                logical_top_folder=logical_top_folder,
            )
            batch: List[StorageEntry] = []
            for entry in entries:
                batch.append(entry)
                if len(batch) >= 5000:
                    insert_storage_batch(connection, batch)
                    connection.commit()
                    batch.clear()
            if batch:
                insert_storage_batch(connection, batch)
                connection.commit()

            connection.executemany(
                """
                INSERT INTO scan_issues(source_root, path, issue_type, details)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        issue["source_root"],
                        issue["path"],
                        issue["issue_type"],
                        issue["details"],
                    )
                    for issue in issues
                ],
            )
            connection.commit()
            root_key = str(root)
            if root_key not in stats_by_root:
                stats_by_root[root_key] = dict(stats)
            else:
                combined = stats_by_root[root_key]
                for key, value in stats.items():
                    if key != "source_root" and isinstance(value, int):
                        combined[key] = int(combined.get(key, 0)) + value
            eprint(
                f"[scan] completed {label}: {stats['files']:,} files, "
                f"{human_bytes(stats['bytes'])}, errors={stats['errors']}, "
                f"non_utf8_paths={stats['non_utf8_paths']}"
            )

        all_stats = [stats_by_root[key] for key in sorted(stats_by_root)]
        set_metadata(connection, "storage_roots", [str(root) for root in roots])
        set_metadata(connection, "storage_scan_stats", all_stats)
        if mapping_value:
            set_metadata(connection, "storage_discovery_mapping", str(Path(mapping_value).resolve()))
        set_metadata(connection, "storage_scanned_at", dt.datetime.now().isoformat())
        set_metadata(connection, "tool_version", VERSION)
    eprint(f"[scan] database: {db_path}")
    return 0


def initialize_snapshot_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE snapshot_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE snapshot_files (
            id INTEGER PRIMARY KEY,
            absolute_path TEXT NOT NULL,
            absolute_path_bytes BLOB NOT NULL,
            relative_path TEXT NOT NULL,
            relative_path_bytes BLOB NOT NULL,
            path_encoding_valid INTEGER NOT NULL,
            project_folder TEXT NOT NULL,
            norm_project_folder TEXT NOT NULL,
            filename TEXT NOT NULL,
            norm_filename TEXT NOT NULL,
            norm_path TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            UNIQUE(relative_path_bytes)
        );

        CREATE TABLE snapshot_issues (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL,
            issue_type TEXT NOT NULL,
            details TEXT NOT NULL
        );
        """
    )
    connection.execute(f"PRAGMA user_version={SNAPSHOT_SCHEMA_VERSION}")
    connection.commit()


def set_snapshot_metadata(
    connection: sqlite3.Connection, key: str, value: object
) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO snapshot_metadata(key, value) VALUES (?, ?)",
        (key, str(value)),
    )


def insert_snapshot_files(
    connection: sqlite3.Connection, entries: Sequence[StorageEntry]
) -> None:
    connection.executemany(
        """
        INSERT INTO snapshot_files (
            absolute_path, absolute_path_bytes, relative_path, relative_path_bytes,
            path_encoding_valid, project_folder, norm_project_folder,
            filename, norm_filename, norm_path, size_bytes, mtime_ns
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                entry.absolute_path,
                sqlite3.Binary(entry.absolute_path_bytes),
                entry.relative_path,
                sqlite3.Binary(entry.relative_path_bytes),
                entry.path_encoding_valid,
                entry.top_folder,
                entry.norm_top_folder,
                entry.filename,
                entry.norm_filename,
                entry.norm_path,
                entry.size_bytes,
                entry.mtime_ns,
            )
            for entry in entries
        ],
    )


def command_scan_storage(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Volume root not found: {root}")
    if output.exists():
        raise FileExistsError(
            f"Snapshot output already exists; choose a new path: {output}"
        )
    ensure_not_nested(output, [root])
    require_read_only_roots(
        [root], args.allow_rw_source, operation="scan-storage"
    )
    require_local_storage_root(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    started_at = dt.datetime.now().isoformat()
    snapshot_id = str(getattr(args, "snapshot_id", "") or uuid.uuid4())
    connection = sqlite3.connect(str(output))
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=NORMAL")
        initialize_snapshot_schema(connection)
        metadata = {
            "snapshot_id": snapshot_id,
            "status": "SCANNING",
            "server_hostname": args.server,
            "server_ip": args.server_ip,
            "volume_name": args.volume,
            "volume_root": str(root),
            "started_at": started_at,
            "tool_version": VERSION,
        }
        for key, value in metadata.items():
            set_snapshot_metadata(connection, key, value)
        connection.commit()

        entries, stats, issues = scan_root(
            root,
            progress_every=args.progress_every,
            quiet=args.quiet,
        )
        batch: List[StorageEntry] = []
        for entry in entries:
            batch.append(entry)
            if len(batch) >= 5000:
                insert_snapshot_files(connection, batch)
                connection.commit()
                batch.clear()
        if batch:
            insert_snapshot_files(connection, batch)
        connection.executemany(
            "INSERT INTO snapshot_issues(path, issue_type, details) VALUES (?, ?, ?)",
            [
                (issue["path"], issue["issue_type"], issue["details"])
                for issue in issues
            ],
        )
        completed_at = dt.datetime.now().isoformat()
        for key, value in {
            "status": "COMPLETE",
            "completed_at": completed_at,
            "file_count": stats["files"],
            "total_size_bytes": stats["bytes"],
            "issue_count": len(issues),
        }.items():
            set_snapshot_metadata(connection, key, value)
        connection.commit()
    finally:
        connection.close()
    eprint(
        f"[scan-storage] snapshot={snapshot_id}, files={stats['files']:,}, "
        f"bytes={stats['bytes']:,}, issues={len(issues):,}, output={output}"
    )
    return 0


def snapshot_metadata(connection: sqlite3.Connection) -> Dict[str, str]:
    return {
        str(row["key"]): str(row["value"])
        for row in connection.execute(
            "SELECT key, value FROM snapshot_metadata ORDER BY key"
        )
    }


def import_storage_snapshot(
    connection: sqlite3.Connection, snapshot_path: Path
) -> str:
    snapshot_path = snapshot_path.expanduser().resolve()
    with connect_db_readonly(snapshot_path) as snapshot:
        version = int(snapshot.execute("PRAGMA user_version").fetchone()[0])
        if version != SNAPSHOT_SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported snapshot schema version {version}: {snapshot_path}"
            )
        metadata = snapshot_metadata(snapshot)
        required = {
            "snapshot_id",
            "status",
            "server_hostname",
            "server_ip",
            "volume_name",
            "volume_root",
            "started_at",
            "completed_at",
            "file_count",
            "total_size_bytes",
            "issue_count",
        }
        missing = required - set(metadata)
        if missing:
            raise RuntimeError(
                f"Snapshot metadata is incomplete ({', '.join(sorted(missing))}): "
                f"{snapshot_path}"
            )
        if metadata["status"] != "COMPLETE":
            raise RuntimeError(f"Snapshot is not complete: {snapshot_path}")
        actual_files, actual_bytes = snapshot.execute(
            "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM snapshot_files"
        ).fetchone()
        actual_issues = snapshot.execute(
            "SELECT COUNT(*) FROM snapshot_issues"
        ).fetchone()[0]
        expected = (
            int(metadata["file_count"]),
            int(metadata["total_size_bytes"]),
            int(metadata["issue_count"]),
        )
        if (actual_files, actual_bytes, actual_issues) != expected:
            raise RuntimeError(
                "Snapshot counts do not match metadata: "
                f"expected={expected}, actual={(actual_files, actual_bytes, actual_issues)}"
            )

        snapshot_id = metadata["snapshot_id"]
        existing = connection.execute(
            """
            SELECT ss.*, s.hostname, s.ip_address, v.name AS volume_name,
                   v.root_path AS volume_root
            FROM storage_scans ss
            JOIN servers s ON s.id = ss.server_id
            JOIN volumes v ON v.id = ss.volume_id
            WHERE ss.snapshot_id = ?
            """,
            (snapshot_id,),
        ).fetchone()
        identity = (
            metadata["server_hostname"],
            metadata["server_ip"],
            metadata["volume_name"],
            metadata["volume_root"],
            expected[0],
            expected[1],
            expected[2],
            version,
        )
        if existing is not None:
            stored_identity = (
                existing["hostname"],
                existing["ip_address"],
                existing["volume_name"],
                existing["volume_root"],
                existing["file_count"],
                existing["total_size_bytes"],
                existing["issue_count"],
                existing["snapshot_schema_version"],
            )
            if stored_identity != identity:
                raise RuntimeError(
                    f"Snapshot identity conflict for {snapshot_id}: {snapshot_path}"
                )
            snapshot_columns = (
                "absolute_path",
                "absolute_path_bytes",
                "relative_path",
                "relative_path_bytes",
                "path_encoding_valid",
                "project_folder",
                "norm_project_folder",
                "filename",
                "norm_filename",
                "norm_path",
                "size_bytes",
                "mtime_ns",
            )
            snapshot_rows = snapshot.execute(
                "SELECT " + ", ".join(snapshot_columns)
                + " FROM snapshot_files ORDER BY relative_path_bytes"
            )
            central_rows = connection.execute(
                "SELECT " + ", ".join(snapshot_columns)
                + " FROM storage_files WHERE scan_id = ? ORDER BY relative_path_bytes",
                (existing["id"],),
            )
            for snapshot_row, central_row in itertools.zip_longest(
                snapshot_rows, central_rows
            ):
                if snapshot_row is None or central_row is None or tuple(snapshot_row) != tuple(central_row):
                    raise RuntimeError(
                        f"Snapshot file inventory conflict for {snapshot_id}: {snapshot_path}"
                    )
            snapshot_issues = snapshot.execute(
                "SELECT path, issue_type, details FROM snapshot_issues ORDER BY id"
            )
            central_issues = connection.execute(
                """
                SELECT path, issue_type, details FROM storage_scan_issues
                WHERE scan_id = ? ORDER BY id
                """,
                (existing["id"],),
            )
            for snapshot_issue, central_issue in itertools.zip_longest(
                snapshot_issues, central_issues
            ):
                if snapshot_issue is None or central_issue is None or tuple(snapshot_issue) != tuple(central_issue):
                    raise RuntimeError(
                        f"Snapshot issue inventory conflict for {snapshot_id}: {snapshot_path}"
                    )
            return "ALREADY_IMPORTED"

        connection.execute("BEGIN IMMEDIATE")
        try:
            now = dt.datetime.now().isoformat()
            server = connection.execute(
                "SELECT id, ip_address FROM servers WHERE hostname = ?",
                (metadata["server_hostname"],),
            ).fetchone()
            if server is None:
                cursor = connection.execute(
                    "INSERT INTO servers(hostname, ip_address, created_at) VALUES (?, ?, ?)",
                    (metadata["server_hostname"], metadata["server_ip"], now),
                )
                server_id = int(cursor.lastrowid)
            else:
                if server["ip_address"] != metadata["server_ip"]:
                    raise RuntimeError(
                        f"Server IP conflict for {metadata['server_hostname']!r}"
                    )
                server_id = int(server["id"])
            volume = connection.execute(
                "SELECT id, root_path FROM volumes WHERE server_id = ? AND name = ?",
                (server_id, metadata["volume_name"]),
            ).fetchone()
            if volume is None:
                cursor = connection.execute(
                    """
                    INSERT INTO volumes(server_id, name, root_path, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (server_id, metadata["volume_name"], metadata["volume_root"], now),
                )
                volume_id = int(cursor.lastrowid)
            else:
                if volume["root_path"] != metadata["volume_root"]:
                    raise RuntimeError(
                        f"Volume root conflict for {metadata['volume_name']!r}"
                    )
                volume_id = int(volume["id"])
            cursor = connection.execute(
                """
                INSERT INTO storage_scans (
                    snapshot_id, server_id, volume_id, source_root, started_at,
                    completed_at, imported_at, file_count, total_size_bytes,
                    issue_count, snapshot_schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    server_id,
                    volume_id,
                    metadata["volume_root"],
                    metadata["started_at"],
                    metadata["completed_at"],
                    now,
                    expected[0],
                    expected[1],
                    expected[2],
                    version,
                ),
            )
            scan_id = int(cursor.lastrowid)
            file_rows = snapshot.execute(
                "SELECT * FROM snapshot_files ORDER BY id"
            )
            batch = []
            for row in file_rows:
                batch.append(
                    (
                        scan_id,
                        server_id,
                        volume_id,
                        row["absolute_path"],
                        row["absolute_path_bytes"],
                        row["relative_path"],
                        row["relative_path_bytes"],
                        row["path_encoding_valid"],
                        row["project_folder"],
                        row["norm_project_folder"],
                        row["filename"],
                        row["norm_filename"],
                        row["norm_path"],
                        row["size_bytes"],
                        row["mtime_ns"],
                    )
                )
                if len(batch) >= 5000:
                    connection.executemany(
                        """
                        INSERT INTO storage_files (
                            scan_id, server_id, volume_id, absolute_path,
                            absolute_path_bytes, relative_path, relative_path_bytes,
                            path_encoding_valid, project_folder, norm_project_folder,
                            filename, norm_filename, norm_path, size_bytes, mtime_ns
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        batch,
                    )
                    batch.clear()
            if batch:
                connection.executemany(
                    """
                    INSERT INTO storage_files (
                        scan_id, server_id, volume_id, absolute_path,
                        absolute_path_bytes, relative_path, relative_path_bytes,
                        path_encoding_valid, project_folder, norm_project_folder,
                        filename, norm_filename, norm_path, size_bytes, mtime_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    batch,
                )
            connection.executemany(
                """
                INSERT INTO storage_scan_issues(scan_id, path, issue_type, details)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (scan_id, row["path"], row["issue_type"], row["details"])
                    for row in snapshot.execute(
                        "SELECT path, issue_type, details FROM snapshot_issues ORDER BY id"
                    )
                ],
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return "IMPORTED"


def command_import_storage_scan(args: argparse.Namespace) -> int:
    db_path = Path(args.db).expanduser().resolve()
    snapshot_paths = [Path(item).expanduser().resolve() for item in args.snapshot]
    with connect_db(db_path) as connection:
        for snapshot_path in snapshot_paths:
            result = import_storage_snapshot(connection, snapshot_path)
            eprint(f"[import-storage-scan] {snapshot_path}: {result}")
    return 0


def command_init_db(args: argparse.Namespace) -> int:
    db_path = Path(args.db).expanduser().resolve()
    with connect_db(db_path) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    eprint(f"[init-db] schema_version={version}, database={db_path}")
    return 0


XLSX_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
XLSX_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XLSX_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CASSETTE_GENERATION_RE = re.compile(r"^([A-Z]{2,5}[0-9]{3,7})L([5-9])$")


def normalize_cassette_label(value: str) -> str:
    """Normalize only unambiguous tape-generation suffixes such as FF7480L7."""
    cleaned = unicodedata.normalize("NFC", value).strip().upper()
    match = CASSETTE_GENERATION_RE.fullmatch(cleaned)
    if match:
        cleaned = match.group(1)
    return normalize_component(cleaned)


def xlsx_sheet_targets(archive: zipfile.ZipFile) -> List[Tuple[str, str]]:
    main = {"m": XLSX_MAIN_NS, "r": XLSX_REL_NS}
    package = {"p": XLSX_PACKAGE_REL_NS}
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        element.attrib["Id"]: element.attrib["Target"]
        for element in relationships.findall("p:Relationship", package)
    }
    sheets: List[Tuple[str, str]] = []
    sheet_container = workbook.find("m:sheets", main)
    if sheet_container is None:
        return sheets
    relation_key = "{" + XLSX_REL_NS + "}id"
    for sheet in sheet_container:
        target = targets[sheet.attrib[relation_key]].lstrip("/")
        if not target.startswith("xl/"):
            target = "xl/" + target
        sheets.append((sheet.attrib["name"], target))
    return sheets


def xlsx_shared_strings(archive: zipfile.ZipFile) -> List[str]:
    name = "xl/sharedStrings.xml"
    if name not in archive.namelist():
        return []
    strings: List[str] = []
    stack: List[ET.Element] = []
    with archive.open(name) as stream:
        for event, element in ET.iterparse(stream, events=("start", "end")):
            if event == "start":
                stack.append(element)
                continue
            if element.tag == "{" + XLSX_MAIN_NS + "}si":
                strings.append(
                    "".join(
                        node.text or ""
                        for node in element.iter("{" + XLSX_MAIN_NS + "}t")
                    )
                )
                if len(stack) >= 2:
                    stack[-2].remove(element)
            stack.pop()
    return strings


def xlsx_column(reference: str) -> str:
    match = re.match(r"[A-Z]+", reference)
    return match.group(0) if match else ""


def xlsx_cell_text(cell: ET.Element, shared_strings: Sequence[str]) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return "".join(
            node.text or ""
            for node in cell.iter("{" + XLSX_MAIN_NS + "}t")
        )
    value = cell.find("{" + XLSX_MAIN_NS + "}v")
    if value is None or value.text is None:
        return ""
    if cell_type == "s":
        index = int(value.text)
        if index < 0 or index >= len(shared_strings):
            raise RuntimeError(f"Invalid XLSX shared string index: {index}")
        return shared_strings[index]
    return value.text


def iter_xlsx_rows(
    archive: zipfile.ZipFile,
    sheet_target: str,
    shared_strings: Sequence[str],
) -> Iterator[Tuple[int, Dict[str, str]]]:
    row_tag = "{" + XLSX_MAIN_NS + "}row"
    cell_tag = "{" + XLSX_MAIN_NS + "}c"
    stack: List[ET.Element] = []
    with archive.open(sheet_target) as stream:
        for event, element in ET.iterparse(stream, events=("start", "end")):
            if event == "start":
                stack.append(element)
                continue
            if element.tag == row_tag:
                values: Dict[str, str] = {}
                for cell in element.iter(cell_tag):
                    column = xlsx_column(cell.attrib.get("r", ""))
                    value = xlsx_cell_text(cell, shared_strings)
                    if column and value != "":
                        values[column] = value
                row_number = int(element.attrib.get("r", "0"))
                if values:
                    yield row_number, values
                if len(stack) >= 2:
                    stack[-2].remove(element)
            stack.pop()


def archive_import_state(
    connection: sqlite3.Connection, source_kind: str, workbook: Path
) -> Tuple[str, Optional[int]]:
    stat_result = workbook.stat()
    existing = connection.execute(
        "SELECT id, file_size, mtime_ns FROM archive_imports "
        "WHERE source_kind = ? AND source_path = ?",
        (source_kind, str(workbook)),
    ).fetchone()
    if existing is None:
        return "NEW", None
    if (
        int(existing["file_size"]) == stat_result.st_size
        and int(existing["mtime_ns"]) == stat_result.st_mtime_ns
    ):
        return "ALREADY_IMPORTED", int(existing["id"])
    raise RuntimeError(
        f"Previously imported {source_kind} workbook changed in place: {workbook}"
    )


def create_archive_import(
    connection: sqlite3.Connection, source_kind: str, workbook: Path
) -> int:
    stat_result = workbook.stat()
    cursor = connection.execute(
        """
        INSERT INTO archive_imports (
            source_kind, source_path, file_size, mtime_ns, imported_at, row_count
        ) VALUES (?, ?, ?, ?, ?, 0)
        """,
        (
            source_kind,
            str(workbook),
            stat_result.st_size,
            stat_result.st_mtime_ns,
            dt.datetime.now().isoformat(),
        ),
    )
    return int(cursor.lastrowid)


def parse_catalog_size(value: str) -> Optional[int]:
    stripped = value.strip()
    return int(stripped) if stripped.isdigit() else None


def import_archive_catalog(connection: sqlite3.Connection, workbook: Path) -> str:
    workbook = workbook.expanduser().resolve()
    state, _ = archive_import_state(connection, "archive_catalog_files", workbook)
    if state == "ALREADY_IMPORTED":
        return state
    connection.execute("BEGIN IMMEDIATE")
    try:
        import_id = create_archive_import(
            connection, "archive_catalog_files", workbook
        )
        count = 0
        with zipfile.ZipFile(workbook) as archive:
            shared = xlsx_shared_strings(archive)
            for sheet_name, target in xlsx_sheet_targets(archive):
                rows = iter_xlsx_rows(archive, target, shared)
                header_found = False
                sheet_had_rows = False
                columns: Dict[str, str] = {}
                batch = []
                for row_number, values in rows:
                    sheet_had_rows = True
                    normalized_values = {
                        column: normalize_component(value.strip())
                        for column, value in values.items()
                    }
                    if not header_found:
                        reverse = {value: column for column, value in normalized_values.items()}
                        required_headers = {
                            "project name",
                            "cassette label",
                            "path",
                            "filename",
                        }
                        if required_headers.issubset(reverse):
                            columns = {name: reverse[name] for name in required_headers}
                            if "size" in reverse:
                                columns["size"] = reverse["size"]
                            header_found = True
                        continue
                    project = values.get(columns["project name"], "").strip()
                    cassette = values.get(columns["cassette label"], "").strip()
                    path_raw = values.get(columns["path"], "").strip()
                    filename = values.get(columns["filename"], "").strip()
                    size_raw = values.get(columns.get("size", ""), "").strip()
                    if not any((project, cassette, path_raw, filename, size_raw)):
                        continue
                    batch.append(
                        (
                            import_id,
                            str(workbook),
                            sheet_name,
                            row_number,
                            project,
                            normalize_component(project),
                            cassette,
                            normalize_cassette_label(cassette),
                            path_raw,
                            normalize_relative_path(path_raw),
                            filename,
                            normalize_component(filename),
                            size_raw,
                            parse_catalog_size(size_raw),
                        )
                    )
                    if len(batch) >= 5000:
                        connection.executemany(
                            """
                            INSERT INTO archive_catalog_files (
                                import_id, source_workbook, source_sheet, source_row,
                                project_raw, project_norm, cassette_raw, cassette_norm,
                                path_raw, path_norm, filename_raw, filename_norm,
                                size_raw, size_bytes
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            batch,
                        )
                        count += len(batch)
                        batch.clear()
                if batch:
                    connection.executemany(
                        """
                        INSERT INTO archive_catalog_files (
                            import_id, source_workbook, source_sheet, source_row,
                            project_raw, project_norm, cassette_raw, cassette_norm,
                            path_raw, path_norm, filename_raw, filename_norm,
                            size_raw, size_bytes
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        batch,
                    )
                    count += len(batch)
                if not header_found and sheet_had_rows:
                    raise RuntimeError(f"Catalog headers not found on sheet {sheet_name!r}")
        connection.execute(
            "UPDATE archive_imports SET row_count = ? WHERE id = ?",
            (count, import_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return f"IMPORTED:{count}"


def manual_row_two_is_note(value: str) -> bool:
    cleaned = unicodedata.normalize("NFC", value).strip().rstrip("\\/")
    return bool(re.fullmatch(r"[A-Za-zА-Яа-я_ -]+[0-9]+", cleaned))


def import_manual_catalog(connection: sqlite3.Connection, workbook: Path) -> str:
    workbook = workbook.expanduser().resolve()
    state, _ = archive_import_state(connection, "archive_manual_map", workbook)
    if state == "ALREADY_IMPORTED":
        return state
    connection.execute("BEGIN IMMEDIATE")
    try:
        import_id = create_archive_import(connection, "archive_manual_map", workbook)
        count = 0
        with zipfile.ZipFile(workbook) as archive:
            shared = xlsx_shared_strings(archive)
            for sheet_name, target in xlsx_sheet_targets(archive):
                cassette_by_column: Dict[str, str] = {}
                note_by_column: Dict[str, str] = {}
                batch = []
                for row_number, values in iter_xlsx_rows(archive, target, shared):
                    if row_number == 1:
                        cassette_by_column = {
                            column: value.strip()
                            for column, value in values.items()
                            if value.strip()
                        }
                        continue
                    if not cassette_by_column:
                        continue
                    for column in sorted(values):
                        if column not in cassette_by_column:
                            continue
                        raw_value = values[column].strip()
                        if not raw_value:
                            continue
                        if row_number == 2 and manual_row_two_is_note(raw_value):
                            note_by_column[column] = raw_value
                            continue
                        cassette = cassette_by_column[column]
                        batch.append(
                            (
                                import_id,
                                str(workbook),
                                sheet_name,
                                row_number,
                                column,
                                sheet_name,
                                normalize_component(sheet_name),
                                cassette,
                                normalize_cassette_label(cassette),
                                note_by_column.get(column, ""),
                                raw_value,
                                normalize_relative_path(raw_value),
                            )
                        )
                if batch:
                    connection.executemany(
                        """
                        INSERT INTO archive_manual_map (
                            import_id, source_workbook, source_sheet, source_row,
                            source_column, project_raw, project_norm, cassette_raw,
                            cassette_norm, cassette_note_raw, folder_path_raw,
                            folder_path_norm
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        batch,
                    )
                    count += len(batch)
        connection.execute(
            "UPDATE archive_imports SET row_count = ? WHERE id = ?",
            (count, import_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return f"IMPORTED:{count}"


def command_import_archive_catalog(args: argparse.Namespace) -> int:
    workbook = Path(args.file).expanduser().resolve()
    if not workbook.is_file():
        raise FileNotFoundError(f"Archive catalog not found: {workbook}")
    with connect_db(Path(args.db).expanduser().resolve()) as connection:
        result = import_archive_catalog(connection, workbook)
    eprint(f"[import-archive-catalog] {workbook}: {result}")
    return 0


def command_import_manual_catalog(args: argparse.Namespace) -> int:
    workbook = Path(args.file).expanduser().resolve()
    if not workbook.is_file():
        raise FileNotFoundError(f"Manual catalog not found: {workbook}")
    with connect_db(Path(args.db).expanduser().resolve()) as connection:
        result = import_manual_catalog(connection, workbook)
    eprint(f"[import-manual-catalog] {workbook}: {result}")
    return 0


def human_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    units = ["KiB", "MiB", "GiB", "TiB", "PiB"]
    amount = float(value)
    for unit in units:
        amount /= 1024.0
        if abs(amount) < 1024.0 or unit == units[-1]:
            return f"{amount:.2f} {unit}"
    return f"{value} B"


def signed_human_bytes(value: int) -> str:
    """Format a byte difference with an explicit sign and binary units."""
    if value == 0:
        return "0 B"
    sign = "+" if value > 0 else "-"
    return sign + human_bytes(abs(value))


def row_to_lto(row: sqlite3.Row) -> LtoEntry:
    return LtoEntry(
        source_pdf=row["source_pdf"],
        source_page=row["source_page"],
        project=row["project"],
        tape=row["tape"],
        relative_path=row["relative_path"],
        norm_path=row["norm_path"],
        top_folder=row["top_folder"],
        norm_top_folder=row["norm_top_folder"],
        filename=row["filename"],
        norm_filename=row["norm_filename"],
        reported_size=row["reported_size"],
        size_min_bytes=row["size_min_bytes"],
        size_max_bytes=row["size_max_bytes"],
        size_known=row["size_known"],
        entry_kind=row["entry_kind"],
        sequence_id=row["sequence_id"],
    )


def row_to_storage(row: sqlite3.Row) -> StorageEntry:
    return StorageEntry(
        source_root=row["source_root"],
        absolute_path=row["absolute_path"],
        absolute_path_bytes=(
            bytes(row["absolute_path_bytes"])
            if row["absolute_path_bytes"] is not None
            else os.fsencode(row["absolute_path"])
        ),
        relative_path=row["relative_path"],
        relative_path_bytes=(
            bytes(row["relative_path_bytes"])
            if row["relative_path_bytes"] is not None
            else os.fsencode(row["relative_path"])
        ),
        path_encoding_valid=row["path_encoding_valid"],
        norm_path=row["norm_path"],
        top_folder=row["top_folder"],
        norm_top_folder=row["norm_top_folder"],
        filename=row["filename"],
        norm_filename=row["norm_filename"],
        size_bytes=row["size_bytes"],
        mtime_ns=row["mtime_ns"],
    )


def load_grouped_manifests(
    connection: sqlite3.Connection,
) -> Tuple[Dict[str, List[LtoEntry]], Dict[str, List[StorageEntry]]]:
    lto: Dict[str, List[LtoEntry]] = defaultdict(list)
    storage: Dict[str, List[StorageEntry]] = defaultdict(list)
    for row in connection.execute("SELECT * FROM lto_entries ORDER BY norm_path, id"):
        entry = row_to_lto(row)
        lto[entry.norm_path].append(entry)
    for row in connection.execute("SELECT * FROM storage_entries ORDER BY norm_path, id"):
        entry = row_to_storage(row)
        storage[entry.norm_path].append(entry)
    return lto, storage


def lto_ranges_text(entries: Sequence[LtoEntry]) -> str:
    values = []
    for entry in entries:
        if entry.size_known:
            values.append(
                f"{entry.tape}:{entry.reported_size}"
                f"[{entry.size_min_bytes}-{entry.size_max_bytes}]"
            )
        else:
            values.append(f"{entry.tape}:SIZE_UNKNOWN")
    return " | ".join(values)


def unique_join(values: Iterable[str]) -> str:
    return " | ".join(sorted(set(value for value in values if value)))


def csv_writer(path: Path, fieldnames: Sequence[str]) -> Tuple[object, csv.DictWriter]:
    handle = path.open("w", encoding="utf-8-sig", newline="")
    writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    return handle, writer


def export_query_csv(
    connection: sqlite3.Connection, path: Path, query: str, params: Sequence[object] = ()
) -> int:
    cursor = connection.execute(query, params)
    names = [column[0] for column in cursor.description or []]
    count = 0
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(names)
        for row in cursor:
            writer.writerow(tuple(row))
            count += 1
    return count


def lto_duplicate_kind(entries: Sequence[LtoEntry]) -> str:
    """Classify repeated LTO paths without treating identical tape copies as conflicts."""
    if len(entries) <= 1:
        return "none"
    known_ranges = {
        (entry.size_min_bytes, entry.size_max_bytes)
        for entry in entries
        if entry.size_known
    }
    if len(known_ranges) > 1:
        return "conflicting_known_intervals"
    if any(not entry.size_known for entry in entries):
        return "contains_unknown_size"
    return "identical"


def classification_blocking_reasons(
    status: str,
    *,
    storage: StorageEntry,
    lto_kind: str,
    storage_kind: str,
) -> Tuple[str, ...]:
    reasons: List[str] = []
    status_reason = {
        "MISSING_ON_LTO": "MISSING_ON_LTO",
        "SIZE_MISMATCH": "SIZE_MISMATCH",
        "ZERO_SIZE_STORAGE": "ZERO_SIZE",
        "ZERO_SIZE_STORAGE_ONLY": "ZERO_SIZE",
        "MATCH_PATH_ONLY_SIZE_UNKNOWN": "UNKNOWN_LTO_SIZE",
        "AMBIGUOUS_LTO_SIZE_UNKNOWN": "UNKNOWN_LTO_SIZE",
        "POSSIBLE_MOVED_WITHIN_TOP_FOLDER": "POSSIBLE_MOVED",
        "AMBIGUOUS_POSSIBLE_MOVE": "AMBIGUOUS_MATCH",
        "INVALID_FILESYSTEM_ENCODING": "INVALID_FILESYSTEM_ENCODING",
        "CONFLICTING_LTO_ENTRIES": "CONFLICTING_LTO_ENTRIES",
        "AMBIGUOUS_STORAGE_DUPLICATE_SIZES": "DUPLICATE_ON_STORAGE_DIFFERENT_SIZES",
        "ZERO_SIZE_LTO_RECORD": "ZERO_SIZE_LTO_RECORD",
    }.get(status)
    if status_reason:
        reasons.append(status_reason)
    if storage.size_bytes == 0 and "ZERO_SIZE" not in reasons:
        reasons.append("ZERO_SIZE")
    if lto_kind == "conflicting_known_intervals" and "CONFLICTING_LTO_ENTRIES" not in reasons:
        reasons.append("CONFLICTING_LTO_ENTRIES")
    if lto_kind == "contains_unknown_size" and "UNKNOWN_LTO_SIZE" not in reasons:
        reasons.append("UNKNOWN_LTO_SIZE")
    if storage_kind == "different_sizes" and "DUPLICATE_ON_STORAGE_DIFFERENT_SIZES" not in reasons:
        reasons.append("DUPLICATE_ON_STORAGE_DIFFERENT_SIZES")
    return tuple(reasons)


def classify_storage_entries(
    lto_by_path: Dict[str, List[LtoEntry]],
    storage_by_path: Dict[str, List[StorageEntry]],
    *,
    moved_check: bool = True,
) -> List[StorageClassification]:
    """Classify every physical storage row, retaining root and raw path identity."""
    classifications: List[StorageClassification] = []
    pending: List[Tuple[StorageEntry, str]] = []

    def add(
        storage: StorageEntry,
        status: str,
        lto_entries: Sequence[LtoEntry],
        matched: Sequence[LtoEntry],
        note: str,
        storage_kind: str,
    ) -> None:
        lto_kind = lto_duplicate_kind(lto_entries)
        verified = (
            status == "MATCH"
            and bool(matched)
            and storage.size_bytes > 0
            and storage.path_encoding_valid == 1
            and lto_kind not in ("conflicting_known_intervals", "contains_unknown_size")
            and storage_kind != "different_sizes"
        )
        classifications.append(
            StorageClassification(
                storage=storage,
                status=status,
                lto_entries=list(lto_entries),
                matched_lto_entries=list(matched),
                note=note,
                verified_known_size_full_path=verified,
                lto_duplicate_kind=lto_kind,
                storage_duplicate_kind=storage_kind,
                blocking_reasons=classification_blocking_reasons(
                    status,
                    storage=storage,
                    lto_kind=lto_kind,
                    storage_kind=storage_kind,
                ),
            )
        )

    for norm_path in sorted(storage_by_path):
        storage_entries = storage_by_path[norm_path]
        lto_entries = lto_by_path.get(norm_path, [])
        storage_sizes = {entry.size_bytes for entry in storage_entries}
        storage_kind = (
            "none"
            if len(storage_entries) <= 1
            else "identical" if len(storage_sizes) == 1 else "different_sizes"
        )
        lto_kind = lto_duplicate_kind(lto_entries)

        for storage in storage_entries:
            if not storage.path_encoding_valid:
                add(
                    storage,
                    "INVALID_FILESYSTEM_ENCODING",
                    lto_entries,
                    [],
                    "The path contains non-UTF-8 raw filename bytes.",
                    storage_kind,
                )
            elif storage_kind == "different_sizes":
                add(
                    storage,
                    "AMBIGUOUS_STORAGE_DUPLICATE_SIZES",
                    lto_entries,
                    [],
                    "The normalized path exists on storage with different logical sizes.",
                    storage_kind,
                )
            elif storage.size_bytes == 0:
                add(
                    storage,
                    "ZERO_SIZE_STORAGE" if lto_entries else "ZERO_SIZE_STORAGE_ONLY",
                    lto_entries,
                    [],
                    "Storage file is zero bytes.",
                    storage_kind,
                )
            elif lto_entries:
                if lto_kind == "conflicting_known_intervals":
                    add(
                        storage,
                        "CONFLICTING_LTO_ENTRIES",
                        lto_entries,
                        [],
                        "LTO records for this path contain different known size intervals.",
                        storage_kind,
                    )
                    continue

                known_entries = [entry for entry in lto_entries if entry.size_known]
                unknown_entries = [entry for entry in lto_entries if not entry.size_known]
                known_matches = [
                    entry for entry in known_entries if size_matches(storage.size_bytes, entry)
                ]
                if unknown_entries and known_entries:
                    add(
                        storage,
                        "AMBIGUOUS_LTO_SIZE_UNKNOWN",
                        lto_entries,
                        known_matches,
                        "The path has both known-size and unknown-size LTO records.",
                        storage_kind,
                    )
                elif unknown_entries:
                    add(
                        storage,
                        "MATCH_PATH_ONLY_SIZE_UNKNOWN",
                        lto_entries,
                        [],
                        "The LTO path exists but its exact size is unknown.",
                        storage_kind,
                    )
                elif known_matches:
                    note_parts = []
                    if lto_kind == "identical":
                        note_parts.append("Identical LTO copies exist for this path.")
                    if storage_kind == "identical":
                        note_parts.append("Identical storage copies exist on multiple roots.")
                    add(
                        storage,
                        "MATCH",
                        lto_entries,
                        known_matches,
                        " ".join(note_parts),
                        storage_kind,
                    )
                else:
                    add(
                        storage,
                        "SIZE_MISMATCH",
                        lto_entries,
                        [],
                        "The exact path matched, but size is outside every LTO interval.",
                        storage_kind,
                    )
            else:
                pending.append((storage, storage_kind))

    lto_only = {
        norm_path: entries
        for norm_path, entries in lto_by_path.items()
        if norm_path not in storage_by_path
    }
    move_candidates: Dict[Tuple[str, str], List[Tuple[str, List[LtoEntry]]]] = defaultdict(list)
    if moved_check:
        for norm_path, entries in lto_only.items():
            first = entries[0]
            kind = lto_duplicate_kind(entries)
            if kind in ("conflicting_known_intervals", "contains_unknown_size"):
                continue
            if not any(entry.size_known for entry in entries):
                continue
            move_candidates[(first.norm_top_folder, first.norm_filename)].append(
                (norm_path, entries)
            )

    compatible_by_storage: Dict[int, List[Tuple[str, List[LtoEntry]]]] = {}
    lto_candidate_storage_paths: Dict[str, set[str]] = defaultdict(set)
    for storage, _ in pending:
        compatible: List[Tuple[str, List[LtoEntry]]] = []
        for norm_path, entries in move_candidates.get(
            (storage.norm_top_folder, storage.norm_filename), []
        ):
            if any(
                entry.size_known and size_matches(storage.size_bytes, entry)
                for entry in entries
            ):
                compatible.append((norm_path, entries))
                lto_candidate_storage_paths[norm_path].add(storage.norm_path)
        compatible_by_storage[id(storage)] = compatible

    for storage, storage_kind in pending:
        compatible = compatible_by_storage[id(storage)]
        many_to_one = any(
            len(lto_candidate_storage_paths[norm_path]) > 1
            for norm_path, _ in compatible
        )
        if len(compatible) == 1 and not many_to_one:
            _, entries = compatible[0]
            add(
                storage,
                "POSSIBLE_MOVED_WITHIN_TOP_FOLDER",
                entries,
                [],
                f"Storage path differs from LTO path {entries[0].relative_path!r}.",
                storage_kind,
            )
        elif compatible:
            entries = list(itertools.chain.from_iterable(item[1] for item in compatible))
            add(
                storage,
                "AMBIGUOUS_POSSIBLE_MOVE",
                entries,
                [],
                "Multiple compatible moved-file candidates exist.",
                storage_kind,
            )
        else:
            add(
                storage,
                "MISSING_ON_LTO",
                [],
                [],
                "Eligible for copy list.",
                storage_kind,
            )

    return sorted(
        classifications,
        key=lambda item: (
            item.storage.norm_path,
            normalize_component(item.storage.source_root),
            item.storage.relative_path_bytes,
        ),
    )


def compare_manifests(
    connection: sqlite3.Connection,
    out_dir: Path,
    *,
    moved_check: bool = True,
) -> dict:
    lto_by_path, storage_by_path = load_grouped_manifests(connection)

    fieldnames = [
        "status",
        "relative_path",
        "storage_absolute_path",
        "storage_root",
        "storage_size_bytes",
        "storage_size_human",
        "storage_path_encoding_valid",
        "lto_tapes",
        "lto_reported_sizes",
        "lto_source_pdfs",
        "note",
    ]

    outputs = {}
    for filename in [
        "all_results.csv",
        "matches.csv",
        "size_mismatches.csv",
        "missing_on_lto.csv",
        "missing_on_storage.csv",
        "possible_moved.csv",
        "ambiguous.csv",
        "zero_size.csv",
    ]:
        handle, writer = csv_writer(out_dir / filename, fieldnames)
        outputs[filename] = (handle, writer)

    summary = defaultdict(int)

    def base_row(
        status: str,
        storage_entries: Sequence[StorageEntry],
        lto_entries: Sequence[LtoEntry],
        note: str = "",
        relative_path: str = "",
    ) -> dict:
        storage = storage_entries[0] if storage_entries else None
        if not relative_path:
            if storage:
                relative_path = storage.relative_path
            elif lto_entries:
                relative_path = lto_entries[0].relative_path
        return {
            "status": status,
            "relative_path": relative_path,
            "storage_absolute_path": storage.absolute_path if storage else "",
            "storage_root": storage.source_root if storage else "",
            "storage_size_bytes": storage.size_bytes if storage else "",
            "storage_size_human": human_bytes(storage.size_bytes) if storage else "",
            "storage_path_encoding_valid": storage.path_encoding_valid if storage else "",
            "lto_tapes": unique_join(entry.tape for entry in lto_entries),
            "_storage_absolute_path_bytes": storage.absolute_path_bytes if storage else b"",
            "_storage_relative_path_bytes": storage.relative_path_bytes if storage else b"",
            "lto_reported_sizes": lto_ranges_text(lto_entries),
            "lto_source_pdfs": unique_join(entry.source_pdf for entry in lto_entries),
            "note": note,
        }

    classifications = classify_storage_entries(
        lto_by_path, storage_by_path, moved_check=moved_check
    )
    classifications_by_path: Dict[str, List[StorageClassification]] = defaultdict(list)
    for classification in classifications:
        classifications_by_path[classification.storage.norm_path].append(classification)

    missing_on_lto_rows: List[dict] = []
    used_moved_lto_paths: set[str] = set()
    for norm_path in sorted(classifications_by_path):
        path_classifications = classifications_by_path[norm_path]
        representative = path_classifications[0]
        storage_entries = [item.storage for item in path_classifications]
        lto_entries = representative.lto_entries
        status = representative.status
        row = base_row(status, storage_entries, lto_entries, note=representative.note)
        outputs["all_results.csv"][1].writerow(row)
        summary[status] += 1

        if status == "MATCH":
            outputs["matches.csv"][1].writerow(row)
        elif status in ("MATCH_PATH_ONLY_SIZE_UNKNOWN", "AMBIGUOUS_LTO_SIZE_UNKNOWN"):
            outputs["matches.csv"][1].writerow(row)
            outputs["ambiguous.csv"][1].writerow(row)
        elif status == "SIZE_MISMATCH":
            outputs["size_mismatches.csv"][1].writerow(row)
        elif status == "MISSING_ON_LTO":
            outputs["missing_on_lto.csv"][1].writerow(row)
            missing_on_lto_rows.append(row)
        elif status == "POSSIBLE_MOVED_WITHIN_TOP_FOLDER":
            outputs["possible_moved.csv"][1].writerow(row)
            used_moved_lto_paths.update(entry.norm_path for entry in lto_entries)
        elif status in (
            "ZERO_SIZE_STORAGE",
            "ZERO_SIZE_STORAGE_ONLY",
        ):
            outputs["zero_size.csv"][1].writerow(row)
        else:
            outputs["ambiguous.csv"][1].writerow(row)
            if status == "AMBIGUOUS_POSSIBLE_MOVE":
                used_moved_lto_paths.update(entry.norm_path for entry in lto_entries)

        exact_lto_entries = lto_by_path.get(norm_path, [])
        zero_lto_entries = [
            entry
            for entry in exact_lto_entries
            if entry.size_known
            and entry.size_min_bytes == 0
            and entry.size_max_bytes == 0
        ]
        if zero_lto_entries:
            zero_row = base_row(
                "ZERO_SIZE_LTO_RECORD",
                storage_entries,
                zero_lto_entries,
                note="At least one LTO manifest record is zero bytes.",
            )
            outputs["zero_size.csv"][1].writerow(zero_row)
            summary["ZERO_SIZE_LTO_RECORD"] += 1

    lto_only = {
        norm_path: entries
        for norm_path, entries in lto_by_path.items()
        if norm_path not in storage_by_path
    }
    for norm_path, lto_entries in sorted(lto_only.items()):
        if norm_path in used_moved_lto_paths:
            continue
        zero_entries = [
            entry
            for entry in lto_entries
            if entry.size_known
            and entry.size_min_bytes == 0
            and entry.size_max_bytes == 0
        ]
        nonzero_entries = [entry for entry in lto_entries if entry not in zero_entries]
        status = "ZERO_SIZE_LTO_ONLY" if zero_entries and not nonzero_entries else "MISSING_ON_STORAGE"
        row = base_row(
            status,
            [],
            lto_entries,
            note=(
                "All LTO records for this path are zero bytes; source file is absent."
                if status == "ZERO_SIZE_LTO_ONLY"
                else "Present in YoYotta manifest but absent under every storage root."
            ),
        )
        outputs["all_results.csv"][1].writerow(row)
        if zero_entries:
            zero_row = base_row(
                "ZERO_SIZE_LTO_RECORD",
                [],
                zero_entries,
                note="Zero-size LTO manifest record; source file is absent.",
            )
            outputs["zero_size.csv"][1].writerow(zero_row)
            summary["ZERO_SIZE_LTO_RECORD"] += 1
        if status == "ZERO_SIZE_LTO_ONLY":
            summary[status] += 1
        else:
            outputs["missing_on_storage.csv"][1].writerow(row)
            summary[status] += 1

    for handle, _ in outputs.values():
        handle.close()

    write_copy_lists(out_dir, missing_on_lto_rows)

    # Detailed duplicate/overlap reports.
    lto_duplicate_count = export_query_csv(
        connection,
        out_dir / "lto_duplicate_paths.csv",
        """
        SELECT
            norm_path,
            MIN(relative_path) AS example_relative_path,
            COUNT(*) AS manifest_records,
            COUNT(DISTINCT tape) AS tape_count,
            GROUP_CONCAT(DISTINCT tape) AS tapes,
            GROUP_CONCAT(DISTINCT source_pdf) AS source_pdfs,
            COUNT(DISTINCT COALESCE(CAST(size_min_bytes AS TEXT), 'UNKNOWN') || ':' ||
                                   COALESCE(CAST(size_max_bytes AS TEXT), 'UNKNOWN'))
                AS distinct_size_ranges
        FROM lto_entries
        GROUP BY norm_path
        HAVING COUNT(*) > 1
        ORDER BY norm_path
        """,
    )
    manifest_overlap_count = export_query_csv(
        connection,
        out_dir / "duplicate_manifest_locations.csv",
        """
        SELECT
            tape,
            norm_path,
            MIN(relative_path) AS example_relative_path,
            COUNT(*) AS records,
            GROUP_CONCAT(DISTINCT source_pdf) AS source_pdfs,
            COUNT(DISTINCT COALESCE(CAST(size_min_bytes AS TEXT), 'UNKNOWN') || ':' ||
                                   COALESCE(CAST(size_max_bytes AS TEXT), 'UNKNOWN'))
                AS distinct_size_ranges
        FROM lto_entries
        GROUP BY tape, norm_path
        HAVING COUNT(*) > 1
        ORDER BY tape, norm_path
        """,
    )
    storage_duplicate_count = export_query_csv(
        connection,
        out_dir / "storage_duplicate_paths.csv",
        """
        SELECT
            norm_path,
            MIN(relative_path) AS example_relative_path,
            COUNT(*) AS copies,
            GROUP_CONCAT(DISTINCT source_root) AS source_roots,
            GROUP_CONCAT(absolute_path, ' | ') AS absolute_paths,
            COUNT(DISTINCT size_bytes) AS distinct_sizes
        FROM storage_entries
        GROUP BY norm_path
        HAVING COUNT(*) > 1
        ORDER BY norm_path
        """,
    )

    parse_issue_count = export_query_csv(
        connection,
        out_dir / "parse_issues.csv",
        "SELECT source_pdf, page, issue_type, details FROM parse_issues ORDER BY source_pdf, page, id",
    )
    scan_issue_count = export_query_csv(
        connection,
        out_dir / "scan_issues.csv",
        "SELECT source_root, path, issue_type, details FROM scan_issues ORDER BY source_root, path, id",
    )
    export_query_csv(
        connection,
        out_dir / "report_stats.csv",
        "SELECT * FROM report_stats ORDER BY source_pdf",
    )
    export_query_csv(
        connection,
        out_dir / "lto_manifest.csv",
        """
        SELECT project, tape, relative_path, reported_size, size_min_bytes,
               size_max_bytes, size_known, entry_kind, sequence_id,
               source_pdf, source_page
        FROM lto_entries
        ORDER BY norm_path, tape, source_pdf
        """,
    )
    export_query_csv(
        connection,
        out_dir / "storage_manifest.csv",
        """
        SELECT source_root, absolute_path, relative_path, path_encoding_valid,
               size_bytes, mtime_ns
        FROM storage_entries
        ORDER BY norm_path, source_root
        """,
    )

    summary["LTO_MANIFEST_RECORDS"] = connection.execute(
        "SELECT COUNT(*) FROM lto_entries"
    ).fetchone()[0]
    summary["LTO_UNIQUE_PATHS"] = connection.execute(
        "SELECT COUNT(DISTINCT norm_path) FROM lto_entries"
    ).fetchone()[0]
    summary["STORAGE_FILES"] = connection.execute(
        "SELECT COUNT(*) FROM storage_entries"
    ).fetchone()[0]
    summary["STORAGE_UNIQUE_PATHS"] = connection.execute(
        "SELECT COUNT(DISTINCT norm_path) FROM storage_entries"
    ).fetchone()[0]
    summary["LTO_DUPLICATE_PATH_GROUPS"] = lto_duplicate_count
    summary["DUPLICATE_MANIFEST_LOCATION_GROUPS"] = manifest_overlap_count
    summary["STORAGE_DUPLICATE_PATH_GROUPS"] = storage_duplicate_count
    summary["PARSE_ISSUES"] = parse_issue_count
    summary["SCAN_ISSUES"] = scan_issue_count
    summary["NON_UTF8_STORAGE_PATHS"] = connection.execute(
        "SELECT COUNT(*) FROM storage_entries WHERE path_encoding_valid = 0"
    ).fetchone()[0]
    summary["COPY_LIST_FILES"] = len(missing_on_lto_rows)

    with (out_dir / "summary.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "value"])
        for key in sorted(summary):
            writer.writerow([key, summary[key]])

    with (out_dir / "summary.json").open("w", encoding="utf-8") as handle:
        json.dump(dict(sorted(summary.items())), handle, ensure_ascii=False, indent=2)

    return dict(summary)


def write_copy_lists(out_dir: Path, missing_rows: Sequence[dict]) -> None:
    absolute_rows = [
        (
            str(row["storage_absolute_path"]),
            bytes(row.get("_storage_absolute_path_bytes") or os.fsencode(str(row["storage_absolute_path"]))),
        )
        for row in missing_rows
    ]
    relative_rows = [
        (
            str(row["storage_root"]),
            str(row["relative_path"]),
            bytes(row.get("_storage_relative_path_bytes") or os.fsencode(str(row["relative_path"]))),
        )
        for row in missing_rows
    ]

    with (out_dir / "missing_on_lto_absolute_paths.txt").open(
        "w", encoding="utf-8", errors="backslashreplace", newline="\n"
    ) as handle:
        for display_path, _ in absolute_rows:
            handle.write(display_path + "\n")

    # The NUL list is authoritative for arbitrary Unix filenames. It is written
    # from exact filesystem bytes captured during the scan.
    with (out_dir / "missing_on_lto_absolute_paths.nul").open("wb") as handle:
        for _, raw_path in absolute_rows:
            handle.write(raw_path + b"\0")

    with (out_dir / "missing_on_lto_shell_quoted.txt").open(
        "w", encoding="utf-8", errors="backslashreplace", newline="\n"
    ) as handle:
        for display_path, _ in absolute_rows:
            handle.write(shlex.quote(display_path) + "\n")

    by_root: Dict[str, List[Tuple[str, bytes]]] = defaultdict(list)
    for root, relative_display, relative_raw in relative_rows:
        by_root[root].append((relative_display, relative_raw))

    copy_dir = out_dir / "copy_lists_by_root"
    copy_dir.mkdir(exist_ok=True)
    with (copy_dir / "roots.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["list_id", "source_root", "text_file", "nul_file"])
        for index, root in enumerate(sorted(by_root), start=1):
            list_id = f"root_{index:02d}"
            text_name = f"{list_id}_relative_paths.txt"
            nul_name = f"{list_id}_relative_paths.nul"
            writer.writerow([list_id, root, text_name, nul_name])
            rows = sorted(by_root[root], key=lambda item: normalize_relative_path(item[0]))
            with (copy_dir / text_name).open(
                "w", encoding="utf-8", errors="backslashreplace", newline="\n"
            ) as out:
                for relative_display, _ in rows:
                    out.write(relative_display + "\n")
            with (copy_dir / nul_name).open("wb") as out:
                for _, relative_raw in rows:
                    out.write(relative_raw + b"\0")


def raw_top_folder(relative_path_bytes: bytes) -> bytes:
    return relative_path_bytes.split(b"/", 1)[0] if relative_path_bytes else b""


def issue_folder_key(source_root: str, issue_path: str) -> Optional[Tuple[str, bytes]]:
    """Best-effort localization of a scan issue without touching source storage."""
    root_text = os.path.normpath(source_root)
    path_text = os.path.normpath(issue_path)
    try:
        if os.path.commonpath([root_text, path_text]) != root_text:
            return None
    except ValueError:
        return None
    relative = os.path.relpath(path_text, root_text)
    if relative in ("", ".") or relative == ".." or relative.startswith("../"):
        return None
    top_display = relative.split(os.sep, 1)[0]
    if "\\x" in top_display:
        # Escaped display text is not reversible to an exact physical identity.
        return None
    return source_root, os.fsencode(top_display)


def load_scan_stats(connection: sqlite3.Connection) -> Dict[str, int]:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key = 'storage_scan_stats'"
    ).fetchone()
    if not row:
        return {}
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(value, list):
        return {}
    result: Dict[str, int] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        root = item.get("source_root")
        files = item.get("files")
        if isinstance(root, str) and isinstance(files, int):
            result[root] = files
    return result


def summarize_lto_size_groups(
    groups: Iterable[Sequence[LtoEntry]],
) -> LtoSizeTotals:
    """Sum one logical contribution per normalized LTO path.

    Identical known intervals contribute once. Conflicting known intervals are
    exposed and omitted because selecting one would make the total misleading.
    Unknown records never contribute bytes, including when a known duplicate is
    available for the same path.
    """
    totals = LtoSizeTotals()
    for entries in groups:
        if not entries:
            continue
        totals.path_count += 1
        ranges: Dict[Tuple[int, int], set[str]] = defaultdict(set)
        has_unknown = False
        for entry in entries:
            if entry.size_known:
                assert entry.size_min_bytes is not None
                assert entry.size_max_bytes is not None
                ranges[(entry.size_min_bytes, entry.size_max_bytes)].add(entry.tape)
            else:
                has_unknown = True
        totals.additional_identical_tape_copy_count += sum(
            max(0, len(tapes) - 1) for tapes in ranges.values()
        )
        if has_unknown:
            totals.unknown_size_count += 1
        if len(ranges) > 1:
            totals.conflicting_size_path_count += 1
            continue
        if len(ranges) == 1:
            (minimum, maximum), = ranges
            totals.known_size_count += 1
            totals.total_min_bytes += minimum
            totals.total_max_bytes += maximum
    return totals


def folder_interval_result(
    storage_total: int,
    totals: LtoSizeTotals,
    *,
    no_paths_result: str,
) -> str:
    if totals.path_count == 0:
        return no_paths_result
    if totals.unknown_size_count or totals.conflicting_size_path_count:
        return "UNKNOWN_LTO_SIZES"
    if totals.total_min_bytes <= storage_total <= totals.total_max_bytes:
        return "WITHIN_INTERVAL"
    return "OUTSIDE_INTERVAL"


def build_folder_audits(connection: sqlite3.Connection) -> List[FolderAudit]:
    lto_by_path, storage_by_path = load_grouped_manifests(connection)
    classifications = classify_storage_entries(lto_by_path, storage_by_path)
    folders: Dict[Tuple[str, bytes], FolderAudit] = {}

    for classification in classifications:
        storage = classification.storage
        top_raw = raw_top_folder(storage.relative_path_bytes)
        key = (storage.source_root, top_raw)
        folder = folders.get(key)
        if folder is None:
            folder = FolderAudit(
                source_root=storage.source_root,
                top_folder_bytes=top_raw,
                top_folder=safe_filesystem_display(os.fsdecode(top_raw)),
                norm_top_folder=storage.norm_top_folder,
                classifications=[],
                blocking_reasons=[],
                scan_issue_types=[],
                matched_lto_totals=LtoSizeTotals(),
                manifest_lto_totals=LtoSizeTotals(),
                storage_norm_paths=set(),
                manifest_norm_paths=set(),
            )
            folders[key] = folder
        folder.classifications.append(classification)
        folder.blocking_reasons.extend(classification.blocking_reasons)
        if not classification.verified_known_size_full_path and not classification.blocking_reasons:
            folder.blocking_reasons.append("INCOMPLETE_CLASSIFICATION")
        if not top_raw:
            folder.blocking_reasons.append("INCOMPLETE_CLASSIFICATION")

    issues = list(
        connection.execute(
            "SELECT source_root, path, issue_type, details "
            "FROM scan_issues ORDER BY source_root, path, issue_type, id"
        )
    )
    root_wide_issues: Dict[str, List[str]] = defaultdict(list)
    for issue in issues:
        issue_type = str(issue["issue_type"])
        if issue_type == "non_utf8_filesystem_name":
            continue
        key = issue_folder_key(str(issue["source_root"]), str(issue["path"]))
        reason = issue_type.upper()
        if key is None:
            root_wide_issues[str(issue["source_root"])].append(reason)
            continue
        folder = folders.get(key)
        if folder is None:
            folder = FolderAudit(
                source_root=key[0],
                top_folder_bytes=key[1],
                top_folder=safe_filesystem_display(os.fsdecode(key[1])),
                norm_top_folder=normalize_component(os.fsdecode(key[1])),
                classifications=[],
                blocking_reasons=[],
                scan_issue_types=[],
                matched_lto_totals=LtoSizeTotals(),
                manifest_lto_totals=LtoSizeTotals(),
                storage_norm_paths=set(),
                manifest_norm_paths=set(),
            )
            folders[key] = folder
        folder.blocking_reasons.append(reason)
        folder.scan_issue_types.append(issue_type)

    for folder in folders.values():
        for reason in root_wide_issues.get(folder.source_root, []):
            folder.blocking_reasons.append(reason)
            folder.scan_issue_types.append(reason.lower())

    expected_by_root = load_scan_stats(connection)
    actual_by_root: Dict[str, int] = defaultdict(int)
    for classification in classifications:
        actual_by_root[classification.storage.source_root] += 1
    for root, expected in expected_by_root.items():
        if actual_by_root.get(root, 0) != expected:
            for folder in folders.values():
                if folder.source_root == root:
                    folder.blocking_reasons.append("INCOMPLETE_CLASSIFICATION")

    lto_by_top_folder: Dict[str, Dict[str, List[LtoEntry]]] = defaultdict(dict)
    for norm_path, entries in lto_by_path.items():
        if entries:
            lto_by_top_folder[entries[0].norm_top_folder][norm_path] = entries
    for folder in folders.values():
        folder.storage_norm_paths = {
            item.storage.norm_path for item in folder.classifications
        }
        manifest_groups = lto_by_top_folder.get(folder.norm_top_folder, {})
        folder.manifest_norm_paths = set(manifest_groups)
        folder.matched_lto_totals = summarize_lto_size_groups(
            lto_by_path[norm_path]
            for norm_path in sorted(folder.storage_norm_paths)
            if norm_path in lto_by_path
        )
        folder.manifest_lto_totals = summarize_lto_size_groups(
            manifest_groups[norm_path] for norm_path in sorted(manifest_groups)
        )

    return sorted(
        folders.values(),
        key=lambda folder: (
            normalize_component(folder.source_root),
            folder.source_root,
            folder.top_folder_bytes,
        ),
    )


def folder_absolute_bytes(folder: FolderAudit) -> bytes:
    root_raw = os.fsencode(folder.source_root)
    if root_raw == b"/":
        return root_raw + folder.top_folder_bytes
    return root_raw.rstrip(b"/") + b"/" + folder.top_folder_bytes


def folder_report_row(folder: FolderAudit) -> dict:
    classifications = folder.classifications
    status_counts: Dict[str, int] = defaultdict(int)
    tapes = set()
    lto_copy_count = 0
    for classification in classifications:
        status_counts[classification.status] += 1
        tapes.update(entry.tape for entry in classification.matched_lto_entries)
        lto_copy_count += len(classification.matched_lto_entries)
    reasons = sorted(set(folder.blocking_reasons))
    safe = bool(classifications) and not reasons and all(
        item.verified_known_size_full_path for item in classifications
    )
    if not safe and not reasons:
        reasons = ["INCOMPLETE_CLASSIFICATION"]
    absolute_raw = folder_absolute_bytes(folder)
    absolute_display = safe_filesystem_display(os.fsdecode(absolute_raw))
    decision = "SAFE_TO_DELETE" if safe else "BLOCKED"
    storage_total = sum(item.storage.size_bytes for item in classifications)
    matched = folder.matched_lto_totals
    matched_midpoint = (matched.total_min_bytes + matched.total_max_bytes) // 2
    matched_result = folder_interval_result(
        storage_total, matched, no_paths_result="NO_MATCHED_LTO_PATHS"
    )
    manifest = folder.manifest_lto_totals
    manifest_result = folder_interval_result(
        storage_total, manifest, no_paths_result="NO_LTO_FOLDER"
    )
    matched_diff_min = storage_total - matched.total_min_bytes
    matched_diff_max = storage_total - matched.total_max_bytes
    matched_diff_midpoint = storage_total - matched_midpoint
    manifest_diff_min = storage_total - manifest.total_min_bytes
    manifest_diff_max = storage_total - manifest.total_max_bytes
    return {
        "source_root": folder.source_root,
        "folder_name": folder.top_folder,
        "absolute_path": absolute_display,
        "decision": decision,
        "blocking_reasons": " | ".join(reasons),
        "storage_file_count": len(classifications),
        "storage_total_size_bytes": storage_total,
        "storage_total_size_human": human_bytes(storage_total),
        "lto_matched_path_count": matched.path_count,
        "lto_known_size_file_count": matched.known_size_count,
        "lto_unknown_size_file_count": matched.unknown_size_count,
        "lto_conflicting_size_path_count": matched.conflicting_size_path_count,
        "lto_additional_identical_tape_copy_count": (
            matched.additional_identical_tape_copy_count
        ),
        "lto_total_size_min_bytes": matched.total_min_bytes,
        "lto_total_size_max_bytes": matched.total_max_bytes,
        "lto_total_size_midpoint_bytes": matched_midpoint,
        "lto_total_size_min_human": human_bytes(matched.total_min_bytes),
        "lto_total_size_max_human": human_bytes(matched.total_max_bytes),
        "lto_total_size_midpoint_human": human_bytes(matched_midpoint),
        "lto_folder_manifest_file_count": manifest.path_count,
        "lto_folder_manifest_known_size_count": manifest.known_size_count,
        "lto_folder_manifest_unknown_size_count": manifest.unknown_size_count,
        "lto_folder_manifest_conflicting_size_path_count": (
            manifest.conflicting_size_path_count
        ),
        "lto_folder_manifest_additional_identical_tape_copy_count": (
            manifest.additional_identical_tape_copy_count
        ),
        "lto_folder_manifest_total_min_bytes": manifest.total_min_bytes,
        "lto_folder_manifest_total_max_bytes": manifest.total_max_bytes,
        "lto_folder_manifest_total_min_human": human_bytes(manifest.total_min_bytes),
        "lto_folder_manifest_total_max_human": human_bytes(manifest.total_max_bytes),
        "storage_minus_lto_min_bytes": matched_diff_min,
        "storage_minus_lto_max_bytes": matched_diff_max,
        "storage_minus_lto_midpoint_bytes": matched_diff_midpoint,
        "storage_minus_lto_min_human": signed_human_bytes(matched_diff_min),
        "storage_minus_lto_max_human": signed_human_bytes(matched_diff_max),
        "storage_minus_lto_midpoint_human": signed_human_bytes(matched_diff_midpoint),
        "storage_size_within_lto_folder_interval": (
            "YES"
            if matched_result == "WITHIN_INTERVAL"
            else "NO" if matched_result == "OUTSIDE_INTERVAL" else ""
        ),
        "folder_size_interval_result": matched_result,
        "storage_minus_lto_manifest_min_bytes": manifest_diff_min,
        "storage_minus_lto_manifest_max_bytes": manifest_diff_max,
        "storage_minus_lto_manifest_min_human": signed_human_bytes(manifest_diff_min),
        "storage_minus_lto_manifest_max_human": signed_human_bytes(manifest_diff_max),
        "manifest_folder_size_interval_result": manifest_result,
        # Compatibility aliases retained for existing consumers.
        "status": decision,
        "top_folder": folder.top_folder,
        "absolute_folder": absolute_display,
        "file_count": len(classifications),
        "verified_file_count": sum(
            1 for item in classifications if item.verified_known_size_full_path
        ),
        "total_size_bytes": storage_total,
        "lto_copy_count": lto_copy_count,
        "lto_tapes": " | ".join(sorted(tapes)),
        "classification_counts": json.dumps(
            dict(sorted(status_counts.items())), ensure_ascii=False, sort_keys=True
        ),
        "scan_issue_types": " | ".join(sorted(set(folder.scan_issue_types))),
        "_absolute_folder_bytes": absolute_raw,
    }


SIMPLE_REASON_ORDER = [
    "NO_LTO_FOLDER",
    "UNKNOWN_LTO_SIZE",
    "CONFLICTING_LTO_ENTRIES",
    "STORAGE_ONLY_PATH",
    "LTO_ONLY_PATH",
    "SIZE_MISMATCH",
    "AMBIGUOUS_PATH",
    "FILE_COUNT_MISMATCH",
    "SIZE_OUTSIDE_TOLERANCE",
    "ZERO_SIZE_FILE",
    "SCAN_INCOMPLETE",
    "INVALID_FILESYSTEM_ENCODING",
]


def simple_size_compatible(
    storage_size: int,
    lto_min: int,
    lto_max: int,
    *,
    tolerance_percent: float,
    tolerance_bytes: int,
) -> bool:
    expansion = Decimal(storage_size) * Decimal(str(tolerance_percent)) / Decimal(100)
    within_expanded = (
        Decimal(lto_min) - expansion
        <= Decimal(storage_size)
        <= Decimal(lto_max) + expansion
    )
    if storage_size < lto_min:
        nearest_difference = lto_min - storage_size
    elif storage_size > lto_max:
        nearest_difference = storage_size - lto_max
    else:
        nearest_difference = 0
    return within_expanded or nearest_difference <= tolerance_bytes


def evaluate_simple_folder_result(
    folder: FolderAudit,
    *,
    tolerance_percent: float,
    tolerance_bytes: int,
) -> SimpleFolderEvaluation:
    classifications = folder.classifications
    statuses = [item.status for item in classifications]
    storage_count = len(classifications)
    manifest = folder.manifest_lto_totals
    storage_size = sum(item.storage.size_bytes for item in classifications)

    known_matches = statuses.count("MATCH")
    unknown_matches = statuses.count("MATCH_PATH_ONLY_SIZE_UNKNOWN")
    storage_only = len(folder.storage_norm_paths - folder.manifest_norm_paths)
    lto_only = len(folder.manifest_norm_paths - folder.storage_norm_paths)
    size_mismatches = statuses.count("SIZE_MISMATCH")
    ambiguous_statuses = {
        "POSSIBLE_MOVED_WITHIN_TOP_FOLDER",
        "AMBIGUOUS_POSSIBLE_MOVE",
        "AMBIGUOUS_LTO_SIZE_UNKNOWN",
        "AMBIGUOUS_STORAGE_DUPLICATE_SIZES",
    }
    ambiguous = sum(status in ambiguous_statuses for status in statuses)
    conflicts = manifest.conflicting_size_path_count
    zero_files = sum(item.storage.size_bytes == 0 for item in classifications)
    invalid_encoding = sum(
        not item.storage.path_encoding_valid for item in classifications
    )
    scan_issues = len(folder.scan_issue_types)
    if "INCOMPLETE_CLASSIFICATION" in folder.blocking_reasons and not scan_issues:
        scan_issues = 1

    aggregate_interval_usable = (
        manifest.path_count > 0
        and manifest.known_size_count > 0
        and conflicts == 0
    )
    aggregate_size_ok = (
        simple_size_compatible(
            storage_size,
            manifest.total_min_bytes,
            manifest.total_max_bytes,
            tolerance_percent=tolerance_percent,
            tolerance_bytes=tolerance_bytes,
        )
        if aggregate_interval_usable
        else False
    )
    aggregate_result = (
        "WITHIN_TOLERANCE"
        if aggregate_size_ok
        else "OUTSIDE_TOLERANCE" if aggregate_interval_usable else "UNKNOWN"
    )
    count_ok = storage_count == manifest.path_count
    exact_path_coverage = storage_only == 0 and lto_only == 0
    no_serious_errors = (
        conflicts == 0
        and ambiguous == 0
        and zero_files == 0
        and scan_issues == 0
        and invalid_encoding == 0
        and storage_count > 0
    )

    all_known_verified = known_matches == storage_count
    unknown_only_exception = (
        unknown_matches > 0
        and known_matches + unknown_matches == storage_count
        and all(
            status in ("MATCH", "MATCH_PATH_ONLY_SIZE_UNKNOWN")
            for status in statuses
        )
    )
    file_warning_exception = (
        size_mismatches > 0
        and known_matches + unknown_matches + size_mismatches == storage_count
        and all(
            status in (
                "MATCH",
                "MATCH_PATH_ONLY_SIZE_UNKNOWN",
                "SIZE_MISMATCH",
            )
            for status in statuses
        )
    )
    if (
        all_known_verified
        and count_ok
        and exact_path_coverage
        and no_serious_errors
        and size_mismatches == 0
        and manifest.unknown_size_count == 0
        and aggregate_size_ok
    ):
        result = "YES"
        reasons = ("OK",)
    elif (
        unknown_only_exception
        and count_ok
        and exact_path_coverage
        and no_serious_errors
        and size_mismatches == 0
        and manifest.unknown_size_count > 0
        and aggregate_size_ok
    ):
        result = "YES_WITH_UNKNOWN_SIZE"
        reasons = ("INDIVIDUAL_LTO_SIZE_UNKNOWN_AGGREGATE_MATCH",)
    elif (
        file_warning_exception
        and count_ok
        and exact_path_coverage
        and no_serious_errors
        and aggregate_size_ok
    ):
        result = "YES_WITH_FILE_WARNINGS"
        warning_reasons = ["INDIVIDUAL_SIZE_MISMATCH_AGGREGATE_MATCH"]
        if unknown_matches:
            warning_reasons.append(
                "INDIVIDUAL_LTO_SIZE_UNKNOWN_AGGREGATE_MATCH"
            )
        reasons = tuple(warning_reasons)
    else:
        present_reasons = set()
        if manifest.path_count == 0:
            present_reasons.add("NO_LTO_FOLDER")
        if manifest.unknown_size_count:
            present_reasons.add("UNKNOWN_LTO_SIZE")
        if conflicts:
            present_reasons.add("CONFLICTING_LTO_ENTRIES")
        if storage_only:
            present_reasons.add("STORAGE_ONLY_PATH")
        if lto_only:
            present_reasons.add("LTO_ONLY_PATH")
        if size_mismatches:
            present_reasons.add("SIZE_MISMATCH")
        if ambiguous:
            present_reasons.add("AMBIGUOUS_PATH")
        unsupported_statuses = storage_count - known_matches - unknown_matches
        if unsupported_statuses and not (
            size_mismatches or ambiguous or storage_only or zero_files or invalid_encoding
        ):
            present_reasons.add("AMBIGUOUS_PATH")
        if not count_ok:
            present_reasons.add("FILE_COUNT_MISMATCH")
        if aggregate_interval_usable and not aggregate_size_ok:
            present_reasons.add("SIZE_OUTSIDE_TOLERANCE")
        if zero_files:
            present_reasons.add("ZERO_SIZE_FILE")
        if scan_issues or storage_count == 0:
            present_reasons.add("SCAN_INCOMPLETE")
        if invalid_encoding:
            present_reasons.add("INVALID_FILESYSTEM_ENCODING")
        reasons = tuple(
            reason for reason in SIMPLE_REASON_ORDER if reason in present_reasons
        )
        if not reasons:
            reasons = ("AMBIGUOUS_PATH",)
        result = "NO"

    return SimpleFolderEvaluation(
        result=result,
        reason_codes=reasons,
        known_size_match_count=known_matches,
        unknown_size_path_match_count=unknown_matches,
        storage_only_path_count=storage_only,
        lto_only_path_count=lto_only,
        size_mismatch_count=size_mismatches,
        conflicting_lto_path_count=conflicts,
        ambiguous_path_count=ambiguous,
        zero_size_file_count=zero_files,
        scan_issue_count=scan_issues,
        invalid_encoding_count=invalid_encoding,
        aggregate_size_result=aggregate_result,
    )


def build_simple_folder_rows(
    folders: Sequence[FolderAudit],
    *,
    pdf_size_units: PdfSizeUnitInfo,
    tolerance_percent: float,
    tolerance_bytes: int,
) -> List[dict]:
    rows: List[dict] = []
    for folder in folders:
        evaluation = evaluate_simple_folder_result(
            folder,
            tolerance_percent=tolerance_percent,
            tolerance_bytes=tolerance_bytes,
        )
        storage_count = len(folder.classifications)
        storage_size = sum(
            item.storage.size_bytes for item in folder.classifications
        )
        manifest = folder.manifest_lto_totals
        interval_available = (
            manifest.path_count > 0
            and manifest.known_size_count > 0
            and manifest.conflicting_size_path_count == 0
        )
        midpoint = (manifest.total_min_bytes + manifest.total_max_bytes) // 2
        difference = storage_size - midpoint if interval_available else None
        difference_percent = (
            abs(difference) / max(storage_size, 1) * 100
            if difference is not None
            else None
        )
        count_ok = storage_count == manifest.path_count
        absolute_path = safe_filesystem_display(
            os.fsdecode(folder_absolute_bytes(folder))
        )
        rows.append(
            {
                "folder_name": folder.top_folder,
                "storage_size": human_bytes(storage_size),
                "lto_size": human_bytes(midpoint) if interval_available else "UNKNOWN",
                "storage_file_count": storage_count,
                "lto_file_count": manifest.path_count,
                "result": evaluation.result,
                "source_root": folder.source_root,
                "absolute_path": absolute_path,
                "storage_size_bytes": storage_size,
                "lto_size_min_bytes": (
                    manifest.total_min_bytes if interval_available else ""
                ),
                "lto_size_max_bytes": (
                    manifest.total_max_bytes if interval_available else ""
                ),
                "lto_size_midpoint_bytes": midpoint if interval_available else "",
                "size_difference_bytes": difference if difference is not None else "",
                "size_difference_percent": (
                    f"{difference_percent:.4f}" if difference_percent is not None else ""
                ),
                "size_result": evaluation.aggregate_size_result,
                "file_count_result": "MATCH" if count_ok else "MISMATCH",
                "known_size_match_count": evaluation.known_size_match_count,
                "unknown_size_path_match_count": (
                    evaluation.unknown_size_path_match_count
                ),
                "storage_only_path_count": evaluation.storage_only_path_count,
                "lto_only_path_count": evaluation.lto_only_path_count,
                "size_mismatch_count": evaluation.size_mismatch_count,
                "conflicting_lto_path_count": (
                    evaluation.conflicting_lto_path_count
                ),
                "ambiguous_path_count": evaluation.ambiguous_path_count,
                "zero_size_file_count": evaluation.zero_size_file_count,
                "scan_issue_count": evaluation.scan_issue_count,
                "invalid_encoding_count": evaluation.invalid_encoding_count,
                "aggregate_size_result": evaluation.aggregate_size_result,
                "reason": " | ".join(evaluation.reason_codes),
                "pdf_size_units": pdf_size_units.label,
            }
        )
    return rows


def print_simple_folder_table(
    rows: Sequence[dict],
    *,
    pdf_size_units: PdfSizeUnitInfo,
    tolerance_percent: float,
    tolerance_bytes: int,
) -> None:
    print("Simple folder check settings:")
    print(f"  PDF size unit mode: {pdf_size_units.label}")
    print(f"  Percentage tolerance: {tolerance_percent:g}%")
    print(
        f"  Byte tolerance: {tolerance_bytes} bytes "
        f"({human_bytes(tolerance_bytes)})"
    )
    if pdf_size_units.warning:
        print(f"WARNING: {pdf_size_units.warning}")

    duplicate_names = {
        name
        for name, count in (
            (name, sum(1 for row in rows if row["folder_name"] == name))
            for name in sorted({str(row["folder_name"]) for row in rows})
        )
        if count > 1
    }
    root_labels: Dict[Tuple[str, str], str] = {}
    for folder_name in sorted(duplicate_names):
        roots = sorted(
            {str(row["source_root"]) for row in rows if row["folder_name"] == folder_name}
        )
        basenames = [Path(root).name or root for root in roots]
        labels = (
            basenames
            if len(set(basenames)) == len(basenames)
            else [f"root_{index:02d}" for index in range(1, len(roots) + 1)]
        )
        root_labels.update(
            ((folder_name, root), label)
            for root, label in zip(roots, labels)
        )
    display_rows = []
    for row in rows:
        folder_label = str(row["folder_name"])
        if folder_label in duplicate_names:
            root_id = root_labels[(folder_label, str(row["source_root"]))]
            folder_label += f" [{root_id}]"
        if len(folder_label) > 40:
            folder_label = folder_label[:37] + "..."
        display_rows.append(
            [
                folder_label,
                str(row["storage_size"]),
                str(row["lto_size"]),
                str(row["storage_file_count"]),
                str(row["lto_file_count"]),
                str(row["result"]),
            ]
        )

    headings = ["Folder", "Disk", "LTO", "Disk files", "LTO files", "Result"]
    widths = [
        max([len(headings[index])] + [len(row[index]) for row in display_rows])
        for index in range(len(headings))
    ]

    def table_line(values: Sequence[str]) -> str:
        return "  ".join(
            value.ljust(widths[index]) for index, value in enumerate(values)
        ).rstrip()

    print("")
    print(table_line(headings))
    print(table_line(["-" * width for width in widths]))
    for row in display_rows:
        print(table_line(row))

    result_order = (
        "YES",
        "YES_WITH_UNKNOWN_SIZE",
        "YES_WITH_FILE_WARNINGS",
        "NO",
    )
    grouped_rows = {
        result: [row for row in rows if row["result"] == result]
        for result in result_order
    }
    print("")
    print("Simple folder check:")
    for result in result_order:
        print(f"  {result}: {len(grouped_rows[result])} folders")
    for result in result_order:
        total = sum(
            int(row["storage_size_bytes"]) for row in grouped_rows[result]
        )
        print(f"Total storage size {result}: {human_bytes(total)}")


def write_deletable_folder_reports(
    connection: sqlite3.Connection,
    out_dir: Path,
    *,
    pdf_size_units: Optional[PdfSizeUnitInfo] = None,
    simple_size_tolerance_percent: float = 0.1,
    simple_size_tolerance_bytes: int = 10485760,
) -> dict:
    folders = build_folder_audits(connection)
    rows = [folder_report_row(folder) for folder in folders]
    safe_rows = [row for row in rows if row["status"] == "SAFE_TO_DELETE"]
    blocked_rows = [row for row in rows if row["status"] == "BLOCKED"]
    fieldnames = [
        "source_root",
        "folder_name",
        "absolute_path",
        "decision",
        "blocking_reasons",
        "storage_file_count",
        "storage_total_size_bytes",
        "storage_total_size_human",
        "lto_matched_path_count",
        "lto_known_size_file_count",
        "lto_unknown_size_file_count",
        "lto_conflicting_size_path_count",
        "lto_additional_identical_tape_copy_count",
        "lto_total_size_min_bytes",
        "lto_total_size_max_bytes",
        "lto_total_size_midpoint_bytes",
        "lto_total_size_min_human",
        "lto_total_size_max_human",
        "lto_total_size_midpoint_human",
        "lto_folder_manifest_file_count",
        "lto_folder_manifest_known_size_count",
        "lto_folder_manifest_unknown_size_count",
        "lto_folder_manifest_conflicting_size_path_count",
        "lto_folder_manifest_additional_identical_tape_copy_count",
        "lto_folder_manifest_total_min_bytes",
        "lto_folder_manifest_total_max_bytes",
        "lto_folder_manifest_total_min_human",
        "lto_folder_manifest_total_max_human",
        "storage_minus_lto_min_bytes",
        "storage_minus_lto_max_bytes",
        "storage_minus_lto_midpoint_bytes",
        "storage_minus_lto_min_human",
        "storage_minus_lto_max_human",
        "storage_minus_lto_midpoint_human",
        "storage_size_within_lto_folder_interval",
        "folder_size_interval_result",
        "storage_minus_lto_manifest_min_bytes",
        "storage_minus_lto_manifest_max_bytes",
        "storage_minus_lto_manifest_min_human",
        "storage_minus_lto_manifest_max_human",
        "manifest_folder_size_interval_result",
        "status",
        "top_folder",
        "absolute_folder",
        "file_count",
        "verified_file_count",
        "total_size_bytes",
        "lto_copy_count",
        "lto_tapes",
        "classification_counts",
        "scan_issue_types",
    ]
    for filename, selected in (
        ("deletable_folders.csv", safe_rows),
        ("blocked_folders.csv", blocked_rows),
    ):
        with (out_dir / filename).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(selected)

    with (out_dir / "folder_size_comparison.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    unit_info = pdf_size_units or read_pdf_size_unit_info(connection)
    simple_rows = build_simple_folder_rows(
        folders,
        pdf_size_units=unit_info,
        tolerance_percent=simple_size_tolerance_percent,
        tolerance_bytes=simple_size_tolerance_bytes,
    )
    simple_fieldnames = [
        "folder_name",
        "storage_size",
        "lto_size",
        "storage_file_count",
        "lto_file_count",
        "result",
        "source_root",
        "absolute_path",
        "storage_size_bytes",
        "lto_size_min_bytes",
        "lto_size_max_bytes",
        "lto_size_midpoint_bytes",
        "size_difference_bytes",
        "size_difference_percent",
        "size_result",
        "file_count_result",
        "known_size_match_count",
        "unknown_size_path_match_count",
        "storage_only_path_count",
        "lto_only_path_count",
        "size_mismatch_count",
        "conflicting_lto_path_count",
        "ambiguous_path_count",
        "zero_size_file_count",
        "scan_issue_count",
        "invalid_encoding_count",
        "aggregate_size_result",
        "reason",
        "pdf_size_units",
    ]
    with (out_dir / "simple_folder_check.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=simple_fieldnames)
        writer.writeheader()
        writer.writerows(simple_rows)

    with (out_dir / "deletable_folders.txt").open(
        "w", encoding="utf-8", errors="backslashreplace", newline="\n"
    ) as handle:
        for row in safe_rows:
            handle.write(str(row["absolute_folder"]) + "\n")
    with (out_dir / "deletable_folders.nul").open("wb") as handle:
        for row in safe_rows:
            handle.write(bytes(row["_absolute_folder_bytes"]) + b"\0")
    return {
        "safe": len(safe_rows),
        "blocked": len(blocked_rows),
        "simple_rows": simple_rows,
        "pdf_size_units": unit_info,
    }


def command_deletable_folders(args: argparse.Namespace) -> int:
    db_path = Path(args.db).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    tolerance_percent = float(
        getattr(args, "simple_size_tolerance_percent", 0.1)
    )
    tolerance_bytes = int(
        getattr(args, "simple_size_tolerance_bytes", 10485760)
    )
    if not math.isfinite(tolerance_percent) or tolerance_percent < 0:
        raise ValueError("--simple-size-tolerance-percent must be finite and non-negative")
    if tolerance_bytes < 0:
        raise ValueError("--simple-size-tolerance-bytes must be non-negative")
    with connect_db_readonly(db_path) as connection:
        ensure_report_output_outside_stored_roots(connection, out_dir)
        lto_count = connection.execute("SELECT COUNT(*) FROM lto_entries").fetchone()[0]
        storage_count = connection.execute("SELECT COUNT(*) FROM storage_entries").fetchone()[0]
        if not lto_count:
            raise RuntimeError("Database has no LTO entries. Run extract first.")
        if not storage_count:
            raise RuntimeError("Database has no storage entries. Run scan first.")
        out_dir.mkdir(parents=True, exist_ok=True)
        unit_info = read_pdf_size_unit_info(connection)
        summary = write_deletable_folder_reports(
            connection,
            out_dir,
            pdf_size_units=unit_info,
            simple_size_tolerance_percent=tolerance_percent,
            simple_size_tolerance_bytes=tolerance_bytes,
        )
    print_simple_folder_table(
        summary["simple_rows"],
        pdf_size_units=summary["pdf_size_units"],
        tolerance_percent=tolerance_percent,
        tolerance_bytes=tolerance_bytes,
    )
    eprint(
        f"[deletable-folders] safe={summary['safe']:,}, "
        f"blocked={summary['blocked']:,}, reports={out_dir}"
    )
    return 0


def command_compare(args: argparse.Namespace) -> int:
    db_path = Path(args.db).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    if not db_path.is_file():
        raise FileNotFoundError(f"Audit database not found: {db_path}")
    with connect_db(db_path) as connection:
        ensure_report_output_outside_stored_roots(connection, out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        lto_count = connection.execute("SELECT COUNT(*) FROM lto_entries").fetchone()[0]
        storage_count = connection.execute("SELECT COUNT(*) FROM storage_entries").fetchone()[0]
        if not lto_count:
            raise RuntimeError("Database has no LTO entries. Run extract first.")
        if not storage_count:
            raise RuntimeError("Database has no storage entries. Run scan first.")
        eprint(f"[compare] LTO records={lto_count:,}, storage files={storage_count:,}")
        summary = compare_manifests(
            connection, out_dir, moved_check=not args.no_moved_check
        )
        set_metadata(connection, "compared_at", dt.datetime.now().isoformat())
    eprint(f"[compare] reports: {out_dir}")
    for key in [
        "MATCH",
        "MATCH_PATH_ONLY_SIZE_UNKNOWN",
        "AMBIGUOUS_LTO_SIZE_UNKNOWN",
        "SIZE_MISMATCH",
        "MISSING_ON_LTO",
        "MISSING_ON_STORAGE",
        "POSSIBLE_MOVED_WITHIN_TOP_FOLDER",
        "AMBIGUOUS_POSSIBLE_MOVE",
        "ZERO_SIZE_STORAGE",
        "ZERO_SIZE_STORAGE_ONLY",
        "ZERO_SIZE_LTO_ONLY",
        "ZERO_SIZE_LTO_RECORD",
        "AMBIGUOUS_STORAGE_DUPLICATE_SIZES",
        "CONFLICTING_LTO_ENTRIES",
        "INVALID_FILESYSTEM_ENCODING",
    ]:
        if key in summary:
            eprint(f"[compare] {key}: {summary[key]:,}")
    return 0


def command_all(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir).expanduser().resolve()
    roots = [Path(item).expanduser().resolve() for item in args.root]
    ensure_not_nested(out_dir, roots)
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "audit.sqlite3"

    extract_args = argparse.Namespace(
        pdf=args.pdf,
        db=str(db_path),
        append=False,
        pdf_size_units=args.pdf_size_units,
    )
    command_extract(extract_args)

    scan_args = argparse.Namespace(
        root=args.root,
        db=str(db_path),
        allow_rw_source=args.allow_rw_source,
        progress_every=args.progress_every,
        quiet=args.quiet,
    )
    command_scan(scan_args)

    compare_args = argparse.Namespace(
        db=str(db_path),
        out_dir=str(out_dir),
        no_moved_check=args.no_moved_check,
    )
    return command_compare(compare_args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare YoYotta PDF manifests with Linux storage by case-insensitive "
            "relative path and rounded PDF file size."
        )
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_db = subparsers.add_parser(
        "init-db", help="Create or migrate a central inventory SQLite database"
    )
    init_db.add_argument("--db", required=True, help="Central SQLite database")
    init_db.set_defaults(func=command_init_db)

    scan_storage = subparsers.add_parser(
        "scan-storage", help="Create a portable local metadata inventory snapshot"
    )
    scan_storage.add_argument("--server", required=True, help="Storage server hostname")
    scan_storage.add_argument("--server-ip", required=True, help="Storage server IP")
    scan_storage.add_argument("--volume", required=True, help="Volume name, e.g. VIDEO6")
    scan_storage.add_argument("--root", required=True, help="Local volume mount root")
    scan_storage.add_argument("--output", required=True, help="New snapshot SQLite path")
    scan_storage.add_argument(
        "--allow-rw-source",
        action="store_true",
        help="Allow scanning a volume that is not verified read-only",
    )
    scan_storage.add_argument(
        "--progress-every",
        type=int,
        default=10000,
        help="Print progress every N files (default: 10000; 0 disables)",
    )
    scan_storage.add_argument(
        "--quiet", action="store_true", help="Suppress periodic progress"
    )
    scan_storage.set_defaults(func=command_scan_storage)

    import_scan = subparsers.add_parser(
        "import-storage-scan", help="Import portable storage snapshots into central SQLite"
    )
    import_scan.add_argument(
        "--snapshot", nargs="+", required=True, help="Snapshot SQLite file(s)"
    )
    import_scan.add_argument("--db", required=True, help="Central SQLite database")
    import_scan.set_defaults(func=command_import_storage_scan)

    archive_catalog = subparsers.add_parser(
        "import-archive-catalog",
        help="Import the historical per-file MC2 XLSX catalog",
    )
    archive_catalog.add_argument("--file", required=True, help="MC2 archive XLSX")
    archive_catalog.add_argument("--db", required=True, help="Central SQLite database")
    archive_catalog.set_defaults(func=command_import_archive_catalog)

    manual_catalog = subparsers.add_parser(
        "import-manual-catalog",
        help="Import the historical project/tape/folder XLSX map",
    )
    manual_catalog.add_argument("--file", required=True, help="Manual archive XLSX")
    manual_catalog.add_argument("--db", required=True, help="Central SQLite database")
    manual_catalog.set_defaults(func=command_import_manual_catalog)

    extract = subparsers.add_parser("extract", help="Parse one or more YoYotta PDFs")
    extract.add_argument("--pdf", nargs="+", required=True, help="YoYotta PDF report(s)")
    extract.add_argument("--db", required=True, help="SQLite audit database")
    extract.add_argument(
        "--append",
        action="store_true",
        help="Append reports instead of replacing existing LTO data",
    )
    extract.add_argument(
        "--pdf-size-units",
        choices=sorted(PDF_SIZE_UNIT_TABLES),
        default="decimal",
        help="Interpret YoYotta KB/MB/GB/TB as decimal or binary (default: decimal)",
    )
    extract.set_defaults(func=command_extract)

    discover = subparsers.add_parser(
        "discover",
        help="Map LTO top-level folders to physical folders below RAID SOURCE trees",
    )
    discover.add_argument(
        "--db", required=True, help="Existing SQLite database with extracted LTO records"
    )
    discover.add_argument(
        "--raid-root",
        action="append",
        required=True,
        help="Read-only RAID root; repeat for multiple arrays",
    )
    discover.add_argument("--out-dir", required=True, help="Directory for mapping CSV files")
    discover.add_argument(
        "--max-source-depth",
        type=int,
        default=2,
        help="Maximum depth below a RAID root at which SOURCE may occur (default: 2)",
    )
    discover.add_argument(
        "--anchors-per-folder",
        type=int,
        default=5,
        help="Maximum distinct relative-path anchors per LTO folder (default: 5)",
    )
    discover.add_argument(
        "--min-anchor-matches",
        type=int,
        default=2,
        help="Known-size path anchors required for MATCHED (default: 2)",
    )
    discover.add_argument(
        "--allow-rw-source",
        action="store_true",
        help="Allow discovery on a source mount that is not verified read-only",
    )
    discover.set_defaults(func=command_discover)

    scan = subparsers.add_parser("scan", help="Scan one or more Linux source roots")
    scan_inputs = scan.add_mutually_exclusive_group(required=True)
    scan_inputs.add_argument(
        "--root",
        nargs="+",
        help="Root(s) whose children match paths below /Volumes/<tape>/",
    )
    scan_inputs.add_argument(
        "--mapping",
        help="discovery_mapping.csv; scan only rows whose status is MATCHED",
    )
    scan.add_argument("--db", required=True, help="SQLite audit database")
    scan.add_argument(
        "--allow-rw-source",
        action="store_true",
        help="Allow scanning a source mount that is not verified read-only",
    )
    scan.add_argument(
        "--progress-every",
        type=int,
        default=10000,
        help="Print progress every N files (default: 10000; 0 disables)",
    )
    scan.add_argument("--quiet", action="store_true", help="Suppress periodic progress")
    scan.set_defaults(func=command_scan)

    compare = subparsers.add_parser("compare", help="Compare manifests in the database")
    compare.add_argument("--db", required=True, help="SQLite audit database")
    compare.add_argument("--out-dir", required=True, help="Directory for CSV reports")
    compare.add_argument(
        "--no-moved-check",
        action="store_true",
        help="Disable same-top-folder moved-file candidate detection",
    )
    compare.set_defaults(func=command_compare)

    deletable = subparsers.add_parser(
        "deletable-folders",
        help="Report top-level storage folders fully represented on LTO",
    )
    deletable.add_argument("--db", required=True, help="Existing SQLite audit database")
    deletable.add_argument(
        "--out-dir", required=True, help="Directory for folder safety reports"
    )
    deletable.add_argument(
        "--simple-size-tolerance-percent",
        type=float,
        default=0.1,
        help="Relaxed percent tolerance for simple folder check (default: 0.1)",
    )
    deletable.add_argument(
        "--simple-size-tolerance-bytes",
        type=int,
        default=10485760,
        help="Relaxed byte tolerance for simple folder check (default: 10485760)",
    )
    deletable.set_defaults(func=command_deletable_folders)

    all_cmd = subparsers.add_parser("all", help="Extract, scan and compare in one run")
    all_cmd.add_argument("--pdf", nargs="+", required=True, help="YoYotta PDF report(s)")
    all_cmd.add_argument("--root", nargs="+", required=True, help="Read-only source roots")
    all_cmd.add_argument("--out-dir", required=True, help="Output directory (not under roots)")
    all_cmd.add_argument(
        "--pdf-size-units",
        choices=sorted(PDF_SIZE_UNIT_TABLES),
        default="decimal",
        help="Interpret YoYotta KB/MB/GB/TB as decimal or binary (default: decimal)",
    )
    all_cmd.add_argument(
        "--allow-rw-source",
        action="store_true",
        help="Allow scanning a source mount that is not verified read-only",
    )
    all_cmd.add_argument(
        "--progress-every",
        type=int,
        default=10000,
        help="Print progress every N files (default: 10000; 0 disables)",
    )
    all_cmd.add_argument("--quiet", action="store_true", help="Suppress periodic progress")
    all_cmd.add_argument(
        "--no-moved-check",
        action="store_true",
        help="Disable same-top-folder moved-file candidate detection",
    )
    all_cmd.set_defaults(func=command_all)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        eprint("Interrupted.")
        return 130
    except Exception as exc:
        eprint(f"ERROR: {exc}")
        if os.environ.get("LTO_AUDIT_DEBUG"):
            raise
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
