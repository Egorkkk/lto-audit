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
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

VERSION = "1.0.1"

BINARY_UNITS = {
    "B": 1,
    "KB": 1024,
    "MB": 1024 ** 2,
    "GB": 1024 ** 3,
    "TB": 1024 ** 4,
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


def size_interval(number_text: str, unit: str) -> SizeRange:
    """Convert a rounded YoYotta value to the exact integer-byte interval.

    YoYotta is configured here as binary units. If it displays 23.29 GB, any
    integer byte count that rounds to 23.29 at the shown precision is accepted.
    """
    try:
        value = Decimal(number_text)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid size number: {number_text!r}") from exc

    multiplier = BINARY_UNITS[unit]
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


def parse_yoyotta_pdf(pdf_path: Path) -> Tuple[List[LtoEntry], List[dict], dict]:
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
                size = size_interval(number_text, unit)
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
            first_size = size_interval(number_text, unit)
            last_size = size_interval(last_number, last_unit)
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
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA temp_store=MEMORY")
    initialize_schema(connection)
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
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
    for pdf_path in pdf_paths:
        if not pdf_path.is_file():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

    with connect_db(db_path) as connection:
        if not args.append:
            connection.executescript(
                "DELETE FROM lto_entries; DELETE FROM parse_issues; DELETE FROM report_stats;"
            )
            connection.commit()

        for pdf_path in pdf_paths:
            eprint(f"[extract] {pdf_path}")
            entries, issues, stats = parse_yoyotta_pdf(pdf_path)
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


def require_read_only_roots(roots: Sequence[Path], allow_rw: bool) -> None:
    for root in roots:
        options = mount_options_for(root)
        if options is None:
            message = (
                f"Cannot verify mount options for {root}; findmnt is unavailable or failed."
            )
            if allow_rw:
                eprint(f"[scan] WARNING: {message}")
                continue
            raise RuntimeError(message + " Use a verified RO bind mount or --allow-rw-source.")
        if "ro" not in options:
            if allow_rw:
                eprint(f"[scan] WARNING: source appears writable: {root} ({','.join(options)})")
            else:
                raise RuntimeError(
                    f"Source is not mounted read-only: {root} ({','.join(options)}). "
                    "Use an RO bind mount. Override only deliberately with --allow-rw-source."
                )
        else:
            eprint(f"[scan] verified read-only mount: {root}")


def scan_root(
    root: Path, *, progress_every: int = 10000, quiet: bool = False
) -> Tuple[Iterator[StorageEntry], dict, List[dict]]:
    """Iterative scandir walk; never follows symlinks."""
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
        stack: List[Path] = [root]
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
                raw_relative_text = child_path.relative_to(root).as_posix()
                absolute_path_bytes = os.fsencode(raw_absolute_text)
                relative_path_bytes = os.fsencode(raw_relative_text)
                encoding_valid = not (
                    has_surrogateescape(raw_absolute_text)
                    or has_surrogateescape(raw_relative_text)
                )
                absolute_display = safe_filesystem_display(raw_absolute_text)
                relative_display = safe_filesystem_display(raw_relative_text)
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
    roots = [Path(item).expanduser().resolve() for item in args.root]
    for root in roots:
        if not root.is_dir():
            raise NotADirectoryError(f"Source root not found: {root}")

    ensure_not_nested(db_path, roots)
    require_read_only_roots(roots, args.allow_rw_source)

    with connect_db(db_path) as connection:
        connection.executescript("DELETE FROM storage_entries; DELETE FROM scan_issues;")
        connection.commit()
        all_stats = []
        for root in roots:
            eprint(f"[scan] {root}")
            entries, stats, issues = scan_root(
                root, progress_every=args.progress_every, quiet=args.quiet
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
            all_stats.append(stats)
            eprint(
                f"[scan] completed {root}: {stats['files']:,} files, "
                f"{human_bytes(stats['bytes'])}, errors={stats['errors']}, "
                f"non_utf8_paths={stats['non_utf8_paths']}"
            )

        set_metadata(connection, "storage_roots", [str(root) for root in roots])
        set_metadata(connection, "storage_scan_stats", all_stats)
        set_metadata(connection, "storage_scanned_at", dt.datetime.now().isoformat())
        set_metadata(connection, "tool_version", VERSION)
    eprint(f"[scan] database: {db_path}")
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


def compare_manifests(
    connection: sqlite3.Connection,
    out_dir: Path,
    *,
    moved_check: bool = True,
) -> dict:
    lto_by_path, storage_by_path = load_grouped_manifests(connection)
    all_paths = sorted(set(lto_by_path) | set(storage_by_path))

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
    storage_only: Dict[str, List[StorageEntry]] = {}
    lto_only: Dict[str, List[LtoEntry]] = {}

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

    for norm_path in all_paths:
        lto_entries = lto_by_path.get(norm_path, [])
        storage_entries = storage_by_path.get(norm_path, [])

        if storage_entries and any(not entry.path_encoding_valid for entry in storage_entries):
            row = base_row(
                "INVALID_FILESYSTEM_ENCODING",
                storage_entries,
                lto_entries,
                note=(
                    "The path contains non-UTF-8 raw filename bytes. It was scanned and "
                    "preserved, but is excluded from automatic matching and copy lists. "
                    "Review scan_issues.csv and rename or map it manually if required."
                ),
            )
            outputs["all_results.csv"][1].writerow(row)
            outputs["ambiguous.csv"][1].writerow(row)
            summary["INVALID_FILESYSTEM_ENCODING"] += 1
            continue

        if lto_entries and storage_entries:
            distinct_storage_sizes = {entry.size_bytes for entry in storage_entries}
            if len(distinct_storage_sizes) > 1:
                status = "AMBIGUOUS_STORAGE_DUPLICATE_SIZES"
                row = base_row(
                    status,
                    storage_entries,
                    lto_entries,
                    note=(
                        "The same relative path exists under multiple storage roots with "
                        "different logical sizes; excluded from automatic decisions."
                    ),
                )
                outputs["all_results.csv"][1].writerow(row)
                outputs["ambiguous.csv"][1].writerow(row)
                summary[status] += 1
                continue

            storage = storage_entries[0]
            if storage.size_bytes == 0:
                status = "ZERO_SIZE_STORAGE"
                row = base_row(
                    status,
                    storage_entries,
                    lto_entries,
                    note="Storage file is zero bytes; excluded from normal match logic.",
                )
                outputs["all_results.csv"][1].writerow(row)
                outputs["zero_size.csv"][1].writerow(row)
                summary[status] += 1
                continue

            zero_lto_entries = [
                entry
                for entry in lto_entries
                if entry.size_known
                and entry.size_min_bytes == 0
                and entry.size_max_bytes == 0
            ]
            usable_lto_entries = [entry for entry in lto_entries if entry not in zero_lto_entries]
            if zero_lto_entries:
                zero_row = base_row(
                    "ZERO_SIZE_LTO_RECORD",
                    storage_entries,
                    zero_lto_entries,
                    note=(
                        "At least one LTO manifest copy is zero bytes. Other LTO copies, "
                        "if present, are evaluated separately."
                    ),
                )
                outputs["zero_size.csv"][1].writerow(zero_row)
                summary["ZERO_SIZE_LTO_RECORD"] += 1

            known_matches = [
                entry
                for entry in usable_lto_entries
                if entry.size_known and size_matches(storage.size_bytes, entry)
            ]
            unknown_matches = [entry for entry in usable_lto_entries if not entry.size_known]

            if known_matches:
                status = "MATCH"
                note = ""
                if len(lto_entries) > 1:
                    note = "Path is present in multiple LTO manifest records."
                if len(storage_entries) > 1:
                    note = (note + " " if note else "") + "Path exists under multiple storage roots."
                if zero_lto_entries:
                    note = (note + " " if note else "") + "One or more other LTO records are zero bytes."
                row = base_row(status, storage_entries, lto_entries, note=note)
                outputs["all_results.csv"][1].writerow(row)
                outputs["matches.csv"][1].writerow(row)
                summary[status] += 1
            elif unknown_matches:
                status = "MATCH_PATH_ONLY_SIZE_UNKNOWN"
                row = base_row(
                    status,
                    storage_entries,
                    lto_entries,
                    note="YoYotta sequence entry has no per-file size for this middle frame.",
                )
                outputs["all_results.csv"][1].writerow(row)
                outputs["matches.csv"][1].writerow(row)
                outputs["ambiguous.csv"][1].writerow(row)
                summary[status] += 1
            else:
                status = "SIZE_MISMATCH"
                row = base_row(
                    status,
                    storage_entries,
                    lto_entries,
                    note="Exact relative path matched, but size is outside every YoYotta rounding interval.",
                )
                outputs["all_results.csv"][1].writerow(row)
                outputs["size_mismatches.csv"][1].writerow(row)
                summary[status] += 1
        elif storage_entries:
            distinct_storage_sizes = {entry.size_bytes for entry in storage_entries}
            if len(distinct_storage_sizes) > 1:
                status = "AMBIGUOUS_STORAGE_DUPLICATE_SIZES"
                row = base_row(
                    status,
                    storage_entries,
                    [],
                    note=(
                        "The same relative path exists under multiple storage roots with "
                        "different sizes; excluded from the copy list."
                    ),
                )
                outputs["all_results.csv"][1].writerow(row)
                outputs["ambiguous.csv"][1].writerow(row)
                summary[status] += 1
            elif storage_entries[0].size_bytes == 0:
                status = "ZERO_SIZE_STORAGE_ONLY"
                row = base_row(status, storage_entries, [], note="Not eligible for copy list.")
                outputs["all_results.csv"][1].writerow(row)
                outputs["zero_size.csv"][1].writerow(row)
                summary[status] += 1
            else:
                storage_only[norm_path] = storage_entries
        else:
            lto_only[norm_path] = lto_entries

    moved_storage_paths: set[str] = set()
    moved_lto_paths: set[str] = set()
    ambiguous_storage_paths: set[str] = set()
    ambiguous_lto_paths: set[str] = set()

    if moved_check:
        storage_candidates: Dict[Tuple[str, str], List[str]] = defaultdict(list)
        lto_candidates: Dict[Tuple[str, str], List[str]] = defaultdict(list)
        for norm_path, entries in storage_only.items():
            first = entries[0]
            storage_candidates[(first.norm_top_folder, first.norm_filename)].append(norm_path)
        for norm_path, entries in lto_only.items():
            first = entries[0]
            lto_candidates[(first.norm_top_folder, first.norm_filename)].append(norm_path)

        for key in sorted(set(storage_candidates) & set(lto_candidates)):
            storage_paths = storage_candidates[key]
            lto_paths = lto_candidates[key]
            compatible_pairs: List[Tuple[str, str]] = []
            for storage_path in storage_paths:
                storage_entry = storage_only[storage_path][0]
                for lto_path in lto_paths:
                    lto_entries = lto_only[lto_path]
                    if any(size_matches(storage_entry.size_bytes, entry) for entry in lto_entries):
                        compatible_pairs.append((storage_path, lto_path))

            unique_storage = sorted(set(pair[0] for pair in compatible_pairs))
            unique_lto = sorted(set(pair[1] for pair in compatible_pairs))
            if len(compatible_pairs) == 1 and len(unique_storage) == 1 and len(unique_lto) == 1:
                storage_path, lto_path = compatible_pairs[0]
                storage_entries = storage_only[storage_path]
                lto_entries = lto_only[lto_path]
                row = base_row(
                    "POSSIBLE_MOVED_WITHIN_TOP_FOLDER",
                    storage_entries,
                    lto_entries,
                    relative_path=storage_entries[0].relative_path,
                    note=(
                        f"Storage path differs from LTO path {lto_entries[0].relative_path!r}; "
                        "top-level folder, filename and size match. Review manually."
                    ),
                )
                outputs["all_results.csv"][1].writerow(row)
                outputs["possible_moved.csv"][1].writerow(row)
                summary["POSSIBLE_MOVED_WITHIN_TOP_FOLDER"] += 1
                moved_storage_paths.add(storage_path)
                moved_lto_paths.add(lto_path)
            elif compatible_pairs:
                ambiguous_storage_paths.update(unique_storage)
                ambiguous_lto_paths.update(unique_lto)
                for storage_path in unique_storage:
                    storage_entries = storage_only[storage_path]
                    candidate_lto_paths = [
                        lto_path
                        for s_path, lto_path in compatible_pairs
                        if s_path == storage_path
                    ]
                    lto_entries = list(
                        itertools.chain.from_iterable(lto_only[path] for path in candidate_lto_paths)
                    )
                    row = base_row(
                        "AMBIGUOUS_POSSIBLE_MOVE",
                        storage_entries,
                        lto_entries,
                        note=(
                            "Multiple same-top-folder candidates with the same filename and "
                            "compatible size; excluded from automatic copy list."
                        ),
                    )
                    outputs["all_results.csv"][1].writerow(row)
                    outputs["ambiguous.csv"][1].writerow(row)
                    summary["AMBIGUOUS_POSSIBLE_MOVE"] += 1

    missing_on_lto_rows: List[dict] = []
    for norm_path, storage_entries in sorted(storage_only.items()):
        if norm_path in moved_storage_paths or norm_path in ambiguous_storage_paths:
            continue
        row = base_row(
            "MISSING_ON_LTO",
            storage_entries,
            [],
            note="Eligible for copy list.",
        )
        outputs["all_results.csv"][1].writerow(row)
        outputs["missing_on_lto.csv"][1].writerow(row)
        missing_on_lto_rows.append(row)
        summary["MISSING_ON_LTO"] += 1

    for norm_path, lto_entries in sorted(lto_only.items()):
        if norm_path in moved_lto_paths or norm_path in ambiguous_lto_paths:
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


def command_compare(args: argparse.Namespace) -> int:
    db_path = Path(args.db).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    if not db_path.is_file():
        raise FileNotFoundError(f"Audit database not found: {db_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    with connect_db(db_path) as connection:
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

    extract_args = argparse.Namespace(pdf=args.pdf, db=str(db_path), append=False)
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
            "relative path and rounded binary file size."
        )
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser("extract", help="Parse one or more YoYotta PDFs")
    extract.add_argument("--pdf", nargs="+", required=True, help="YoYotta PDF report(s)")
    extract.add_argument("--db", required=True, help="SQLite audit database")
    extract.add_argument(
        "--append",
        action="store_true",
        help="Append reports instead of replacing existing LTO data",
    )
    extract.set_defaults(func=command_extract)

    scan = subparsers.add_parser("scan", help="Scan one or more Linux source roots")
    scan.add_argument(
        "--root",
        nargs="+",
        required=True,
        help="Root(s) whose children match paths below /Volumes/<tape>/",
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

    all_cmd = subparsers.add_parser("all", help="Extract, scan and compare in one run")
    all_cmd.add_argument("--pdf", nargs="+", required=True, help="YoYotta PDF report(s)")
    all_cmd.add_argument("--root", nargs="+", required=True, help="Read-only source roots")
    all_cmd.add_argument("--out-dir", required=True, help="Output directory (not under roots)")
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
