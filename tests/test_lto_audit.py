import argparse
import csv
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import lto_audit


def storage(root: str, relative: str, size: int, *, valid: int = 1) -> lto_audit.StorageEntry:
    relative_raw = relative.encode("utf-8")
    absolute = f"{root}/{relative}"
    top, filename = lto_audit.path_parts(relative)
    return lto_audit.StorageEntry(
        source_root=root,
        absolute_path=absolute,
        absolute_path_bytes=absolute.encode("utf-8"),
        relative_path=relative,
        relative_path_bytes=relative_raw,
        path_encoding_valid=valid,
        norm_path=lto_audit.normalize_relative_path(relative),
        top_folder=top,
        norm_top_folder=lto_audit.normalize_component(top),
        filename=filename,
        norm_filename=lto_audit.normalize_component(filename),
        size_bytes=size,
        mtime_ns=0,
    )


def lto(relative: str, minimum, maximum, *, tape: str = "T1") -> lto_audit.LtoEntry:
    top, filename = lto_audit.path_parts(relative)
    known = minimum is not None and maximum is not None
    return lto_audit.LtoEntry(
        source_pdf=f"{tape}.pdf",
        source_page=1,
        project="test",
        tape=tape,
        relative_path=relative,
        norm_path=lto_audit.normalize_relative_path(relative),
        top_folder=top,
        norm_top_folder=lto_audit.normalize_component(top),
        filename=filename,
        norm_filename=lto_audit.normalize_component(filename),
        reported_size=str(minimum) if known else "",
        size_min_bytes=minimum,
        size_max_bytes=maximum,
        size_known=int(known),
        entry_kind="regular" if known else "sequence_middle_size_unknown",
        sequence_id="" if known else "sequence-1",
    )


def classify(storage_entries, lto_entries, *, moved_check=True):
    storage_by_path = {}
    lto_by_path = {}
    for entry in storage_entries:
        storage_by_path.setdefault(entry.norm_path, []).append(entry)
    for entry in lto_entries:
        lto_by_path.setdefault(entry.norm_path, []).append(entry)
    return lto_audit.classify_storage_entries(
        lto_by_path, storage_by_path, moved_check=moved_check
    )


class ClassificationTests(unittest.TestCase):
    def test_identical_lto_copies_are_verified(self):
        item = storage("/storage/a", "Project/file.mov", 100)
        results = classify(
            [item],
            [lto(item.relative_path, 100, 100), lto(item.relative_path, 100, 100, tape="T2")],
        )
        self.assertEqual(results[0].status, "MATCH")
        self.assertEqual(results[0].lto_duplicate_kind, "identical")
        self.assertTrue(results[0].verified_known_size_full_path)

    def test_conflicting_lto_copies_never_match(self):
        item = storage("/storage/a", "Project/file.mov", 100)
        results = classify(
            [item],
            [lto(item.relative_path, 100, 100), lto(item.relative_path, 200, 200, tape="T2")],
        )
        self.assertEqual(results[0].status, "CONFLICTING_LTO_ENTRIES")
        self.assertFalse(results[0].verified_known_size_full_path)
    def test_identical_storage_paths_are_classified_per_root(self):
        first = storage("/storage/a", "Project/file.mov", 100)
        second = storage("/storage/b", "Project/file.mov", 100)
        results = classify([first, second], [lto(first.relative_path, 100, 100)])
        self.assertEqual(len(results), 2)
        self.assertEqual({item.storage.source_root for item in results}, {"/storage/a", "/storage/b"})
        self.assertTrue(all(item.status == "MATCH" for item in results))
        self.assertTrue(all(item.storage_duplicate_kind == "identical" for item in results))

    def test_storage_duplicates_with_different_sizes_are_ambiguous(self):
        first = storage("/storage/a", "Project/file.mov", 100)
        second = storage("/storage/b", "Project/file.mov", 200)
        results = classify([first, second], [lto(first.relative_path, 100, 100)])
        self.assertEqual(len(results), 2)
        self.assertTrue(
            all(item.status == "AMBIGUOUS_STORAGE_DUPLICATE_SIZES" for item in results)
        )
        self.assertTrue(all(not item.verified_known_size_full_path for item in results))

    def test_unknown_size_is_not_a_moved_match(self):
        item = storage("/storage/a", "Project/new/file.mov", 100)
        unknown = lto("Project/old/file.mov", None, None)
        results = classify([item], [unknown])
        self.assertEqual(results[0].status, "MISSING_ON_LTO")

    def test_many_storage_paths_to_one_move_candidate_are_ambiguous(self):
        first = storage("/storage/a", "Project/one/file.mov", 100)
        second = storage("/storage/a", "Project/two/file.mov", 100)
        candidate = lto("Project/old/file.mov", 100, 100)
        results = classify([first, second], [candidate])
        self.assertTrue(all(item.status == "AMBIGUOUS_POSSIBLE_MOVE" for item in results))

    def test_known_and_unknown_lto_entries_are_ambiguous(self):
        item = storage("/storage/a", "Project/file.mov", 100)
        results = classify(
            [item], [lto(item.relative_path, 100, 100), lto(item.relative_path, None, None)]
        )
        self.assertEqual(results[0].status, "AMBIGUOUS_LTO_SIZE_UNKNOWN")
        self.assertFalse(results[0].verified_known_size_full_path)


class PdfSizeUnitTests(unittest.TestCase):
    def test_decimal_and_binary_gigabyte_intervals(self):
        decimal = lto_audit.size_interval("1.00", "GB", unit_mode="decimal")
        binary = lto_audit.size_interval("1.00", "GB", unit_mode="binary")
        self.assertLessEqual(decimal.minimum, 1_000_000_000)
        self.assertGreaterEqual(decimal.maximum, 1_000_000_000)
        self.assertLessEqual(binary.minimum, 1_073_741_824)
        self.assertGreaterEqual(binary.maximum, 1_073_741_824)
        self.assertNotEqual(
            (decimal.minimum, decimal.maximum),
            (binary.minimum, binary.maximum),
        )

    def test_new_extract_cli_defaults_to_decimal(self):
        args = lto_audit.build_parser().parse_args(
            ["extract", "--pdf", "report.pdf", "--db", "audit.sqlite3"]
        )
        self.assertEqual(args.pdf_size_units, "decimal")

    def test_extract_persists_units_and_rejects_mixed_append(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            pdf_path = base / "report.pdf"
            pdf_path.write_bytes(b"fixture")
            db_path = base / "audit.sqlite3"
            entry = lto("Project/file.mov", 995_000_000, 1_004_999_999)
            stats = {
                "source_pdf": str(pdf_path.resolve()),
                "project": "fixture",
                "expected_files": 1,
                "extracted_files": 1,
                "difference": 0,
                "sequence_files": 0,
                "size_unknown_files": 0,
                "issues": 0,
            }
            with mock.patch.object(
                lto_audit,
                "parse_yoyotta_pdf",
                return_value=([entry], [], stats),
            ) as parser:
                result = lto_audit.command_extract(
                    argparse.Namespace(
                        pdf=[str(pdf_path)],
                        db=str(db_path),
                        append=False,
                        pdf_size_units="decimal",
                    )
                )
            self.assertEqual(result, 0)
            parser.assert_called_once_with(
                pdf_path.resolve(), pdf_size_units="decimal"
            )
            connection = sqlite3.connect(str(db_path))
            try:
                mode = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'pdf_size_units'"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(mode, "decimal")
            with self.assertRaises(RuntimeError):
                lto_audit.command_extract(
                    argparse.Namespace(
                        pdf=[str(pdf_path)],
                        db=str(db_path),
                        append=True,
                        pdf_size_units="binary",
                    )
                )


class SafetyHelperTests(unittest.TestCase):
    def test_readonly_connection_rejects_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "audit.sqlite3"
            connection = sqlite3.connect(str(db_path))
            connection.execute("CREATE TABLE marker(value TEXT)")
            connection.commit()
            connection.close()
            before_files = sorted(db_path.parent.glob(db_path.name + "*"))
            with lto_audit.connect_db_readonly(db_path) as readonly:
                with self.assertRaises(sqlite3.OperationalError):
                    readonly.execute("INSERT INTO marker VALUES ('changed')")
            self.assertEqual(sorted(db_path.parent.glob(db_path.name + "*")), before_files)

    def test_readonly_connection_refuses_wal_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "audit.sqlite3"
            connection = sqlite3.connect(str(db_path))
            connection.execute("CREATE TABLE marker(value TEXT)")
            connection.commit()
            connection.close()
            Path(str(db_path) + "-wal").write_bytes(b"")
            with self.assertRaises(RuntimeError):
                lto_audit.connect_db_readonly(db_path)

    def test_output_inside_stored_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            root.mkdir()
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            lto_audit.initialize_schema(connection)
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES ('storage_roots', ?)",
                ('["' + str(root) + '"]',),
            )
            with self.assertRaises(RuntimeError):
                lto_audit.ensure_report_output_outside_stored_roots(
                    connection, root / "reports"
                )


class DeletableFoldersTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root_a = self.base / "storage-a"
        self.root_b = self.base / "storage-b"
        self.root_a.mkdir()
        self.root_b.mkdir()
        self.db_path = self.base / "audit.sqlite3"

    def tearDown(self):
        self.temporary.cleanup()

    def create_db(
        self,
        storage_entries,
        lto_entries,
        *,
        issues=(),
        expected=None,
        pdf_size_units="decimal",
    ):
        connection = lto_audit.connect_db(self.db_path)
        try:
            lto_audit.insert_storage_batch(connection, storage_entries)
            lto_audit.insert_lto_data(
                connection,
                lto_entries,
                [],
                {
                    "source_pdf": "fixture.pdf",
                    "project": "fixture",
                    "expected_files": len(lto_entries),
                    "extracted_files": len(lto_entries),
                    "difference": 0,
                    "sequence_files": sum(bool(item.sequence_id) for item in lto_entries),
                    "size_unknown_files": sum(not item.size_known for item in lto_entries),
                    "issues": 0,
                },
            )
            connection.executemany(
                "INSERT INTO scan_issues(source_root, path, issue_type, details) "
                "VALUES (?, ?, ?, ?)",
                issues,
            )
            roots = sorted({item.source_root for item in storage_entries})
            lto_audit.set_metadata(connection, "storage_roots", roots)
            actual = {root: 0 for root in roots}
            for item in storage_entries:
                actual[item.source_root] += 1
            stats = [
                {"source_root": root, "files": (expected or {}).get(root, count)}
                for root, count in sorted(actual.items())
            ]
            lto_audit.set_metadata(connection, "storage_scan_stats", stats)
            if pdf_size_units is not None:
                lto_audit.set_metadata(connection, "pdf_size_units", pdf_size_units)
        finally:
            connection.close()

    def run_report(
        self,
        name="reports",
        *,
        tolerance_percent=0.1,
        tolerance_bytes=10485760,
    ):
        out_dir = self.base / name
        terminal = io.StringIO()
        with redirect_stdout(terminal):
            result = lto_audit.command_deletable_folders(
                argparse.Namespace(
                    db=str(self.db_path),
                    out_dir=str(out_dir),
                    simple_size_tolerance_percent=tolerance_percent,
                    simple_size_tolerance_bytes=tolerance_bytes,
                )
            )
        self.assertEqual(result, 0)
        self.last_terminal_output = terminal.getvalue()
        return out_dir

    def csv_rows(self, path):
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_fully_safe_non_date_unicode_folder_and_raw_nul_output(self):
        folder = "Client Project Юникод"
        item = storage(str(self.root_a), f"{folder}/file.mov", 100)
        self.create_db([item], [lto(item.relative_path, 100, 100)])
        before = hashlib.sha256(self.db_path.read_bytes()).digest()
        before_files = sorted(self.base.glob(self.db_path.name + "*"))
        out_dir = self.run_report()
        after = hashlib.sha256(self.db_path.read_bytes()).digest()
        safe = self.csv_rows(out_dir / "deletable_folders.csv")
        self.assertEqual(before, after)
        self.assertEqual(sorted(self.base.glob(self.db_path.name + "*")), before_files)
        self.assertEqual([row["top_folder"] for row in safe], [folder])
        expected_path = str(self.root_a / folder)
        self.assertEqual((out_dir / "deletable_folders.txt").read_text(), expected_path + "\n")
        self.assertEqual(
            (out_dir / "deletable_folders.nul").read_bytes(),
            expected_path.encode("utf-8") + b"\0",
        )

    def test_exact_folder_totals_and_column_order(self):
        item = storage(str(self.root_a), "Project/file.mov", 1536)
        self.create_db([item], [lto(item.relative_path, 1536, 1536)])
        out_dir = self.run_report()
        row = self.csv_rows(out_dir / "folder_size_comparison.csv")[0]
        with (out_dir / "deletable_folders.csv").open(
            encoding="utf-8-sig", newline=""
        ) as handle:
            columns = next(csv.reader(handle))
        self.assertEqual(
            columns[:5],
            ["source_root", "folder_name", "absolute_path", "decision", "blocking_reasons"],
        )
        self.assertEqual(row["storage_file_count"], "1")
        self.assertEqual(row["storage_total_size_bytes"], "1536")
        self.assertEqual(row["storage_total_size_human"], "1.50 KiB")
        self.assertEqual(row["lto_total_size_min_bytes"], "1536")
        self.assertEqual(row["lto_total_size_max_bytes"], "1536")
        self.assertEqual(row["folder_size_interval_result"], "WITHIN_INTERVAL")
        self.assertEqual(row["storage_minus_lto_midpoint_human"], "0 B")
        simple_path = out_dir / "simple_folder_check.csv"
        simple = self.csv_rows(simple_path)[0]
        with simple_path.open(encoding="utf-8-sig", newline="") as handle:
            simple_columns = next(csv.reader(handle))
        self.assertEqual(
            simple_columns[:6],
            [
                "folder_name",
                "storage_size",
                "lto_size",
                "storage_file_count",
                "lto_file_count",
                "result",
            ],
        )
        self.assertEqual(simple["result"], "YES")
        self.assertEqual(simple["reason"], "OK")
        self.assertEqual(simple["storage_size"], "1.50 KiB")
        self.assertEqual(simple["lto_size"], "1.50 KiB")

    def test_simple_check_marks_known_size_mismatch_as_file_warning(self):
        item = storage(str(self.root_a), "Project/file.mov", 1_000_000)
        self.create_db([item], [lto(item.relative_path, 1_000_500, 1_000_500)])
        row = self.csv_rows(
            self.run_report(tolerance_percent=0.1, tolerance_bytes=0)
            / "simple_folder_check.csv"
        )[0]
        self.assertEqual(row["result"], "YES_WITH_FILE_WARNINGS")
        self.assertEqual(row["size_result"], "WITHIN_TOLERANCE")
        self.assertEqual(row["size_difference_percent"], "0.0500")
        self.assertEqual(row["size_mismatch_count"], "1")
        self.assertEqual(
            row["reason"], "INDIVIDUAL_SIZE_MISMATCH_AGGREGATE_MATCH"
        )
        self.assertEqual(self.csv_rows(self.base / "reports" / "deletable_folders.csv"), [])
        self.assertEqual(
            self.csv_rows(self.base / "reports" / "blocked_folders.csv")[0][
                "decision"
            ],
            "BLOCKED",
        )

    def test_simple_check_byte_tolerance_allows_file_warning(self):
        item = storage(str(self.root_a), "Project/file.mov", 1_000_000)
        self.create_db([item], [lto(item.relative_path, 1_000_005, 1_000_005)])
        row = self.csv_rows(
            self.run_report(tolerance_percent=0, tolerance_bytes=5)
            / "simple_folder_check.csv"
        )[0]
        self.assertEqual(row["result"], "YES_WITH_FILE_WARNINGS")
        self.assertEqual(row["size_difference_bytes"], "-5")
        self.assertEqual(row["aggregate_size_result"], "WITHIN_TOLERANCE")
        self.assertEqual(
            row["reason"], "INDIVIDUAL_SIZE_MISMATCH_AGGREGATE_MATCH"
        )

    def test_simple_check_marks_several_size_mismatches_as_file_warning(self):
        items = [
            storage(str(self.root_a), "Project/one.mov", 100),
            storage(str(self.root_a), "Project/two.mov", 200),
        ]
        self.create_db(
            items,
            [
                lto(items[0].relative_path, 105, 105),
                lto(items[1].relative_path, 195, 195),
            ],
        )
        row = self.csv_rows(
            self.run_report(tolerance_percent=0, tolerance_bytes=0)
            / "simple_folder_check.csv"
        )[0]
        self.assertEqual(row["aggregate_size_result"], "WITHIN_TOLERANCE")
        self.assertEqual(row["size_mismatch_count"], "2")
        self.assertEqual(row["result"], "YES_WITH_FILE_WARNINGS")

    def test_simple_check_accepts_one_exact_unknown_size_for_review(self):
        known = storage(str(self.root_a), "Project/known.mov", 100)
        unknown = storage(str(self.root_a), "Project/unknown.mov", 5)
        self.create_db(
            [known, unknown],
            [lto(known.relative_path, 100, 100), lto(unknown.relative_path, None, None)],
        )
        out_dir = self.run_report(tolerance_percent=0, tolerance_bytes=5)
        row = self.csv_rows(out_dir / "simple_folder_check.csv")[0]
        self.assertEqual(row["result"], "YES_WITH_UNKNOWN_SIZE")
        self.assertEqual(
            row["reason"], "INDIVIDUAL_LTO_SIZE_UNKNOWN_AGGREGATE_MATCH"
        )
        self.assertEqual(row["known_size_match_count"], "1")
        self.assertEqual(row["unknown_size_path_match_count"], "1")
        self.assertEqual(row["storage_only_path_count"], "0")
        self.assertEqual(row["lto_only_path_count"], "0")
        self.assertEqual(row["aggregate_size_result"], "WITHIN_TOLERANCE")
        self.assertEqual(self.csv_rows(out_dir / "deletable_folders.csv"), [])
        self.assertEqual(
            self.csv_rows(out_dir / "blocked_folders.csv")[0]["decision"],
            "BLOCKED",
        )
        self.assertEqual((out_dir / "deletable_folders.txt").read_text(), "")
        self.assertEqual((out_dir / "deletable_folders.nul").read_bytes(), b"")

    def test_simple_check_accepts_multiple_exact_unknown_sizes_for_review(self):
        entries = [storage(str(self.root_a), "Project/known.mov", 100)]
        entries.extend(
            storage(str(self.root_a), f"Project/unknown-{index}.mov", size)
            for index, size in ((1, 2), (2, 3))
        )
        manifest = [lto(entries[0].relative_path, 100, 100)]
        manifest.extend(lto(item.relative_path, None, None) for item in entries[1:])
        self.create_db(entries, manifest)
        row = self.csv_rows(
            self.run_report(tolerance_percent=0, tolerance_bytes=5)
            / "simple_folder_check.csv"
        )[0]
        self.assertEqual(row["result"], "YES_WITH_UNKNOWN_SIZE")
        self.assertEqual(row["unknown_size_path_match_count"], "2")

    def test_unknown_size_with_known_mismatch_uses_file_warning(self):
        known = storage(str(self.root_a), "Project/known.mov", 110)
        unknown = storage(str(self.root_a), "Project/unknown.mov", 5)
        self.create_db(
            [known, unknown],
            [lto(known.relative_path, 100, 100), lto(unknown.relative_path, None, None)],
        )
        row = self.csv_rows(
            self.run_report(tolerance_percent=0, tolerance_bytes=20)
            / "simple_folder_check.csv"
        )[0]
        self.assertEqual(row["result"], "YES_WITH_FILE_WARNINGS")
        self.assertEqual(row["size_mismatch_count"], "1")
        self.assertEqual(row["unknown_size_path_match_count"], "1")
        self.assertEqual(
            row["reason"],
            "INDIVIDUAL_SIZE_MISMATCH_AGGREGATE_MATCH"
            " | INDIVIDUAL_LTO_SIZE_UNKNOWN_AGGREGATE_MATCH",
        )

    def test_size_mismatch_with_zero_size_or_scan_issue_is_no(self):
        for case in ("zero", "scan"):
            with self.subTest(case=case):
                self.db_path = self.base / f"mismatch-{case}.sqlite3"
                first_size = 0 if case == "zero" else 100
                items = [
                    storage(str(self.root_a), "Project/one.mov", first_size),
                    storage(str(self.root_a), "Project/two.mov", 200),
                ]
                first_lto_size = 5 if case == "zero" else 105
                manifest = [
                    lto(items[0].relative_path, first_lto_size, first_lto_size),
                    lto(items[1].relative_path, 195, 195),
                ]
                issues = (
                    (
                        str(self.root_a),
                        str(self.root_a / "Project" / "bad.mov"),
                        "file_stat_error",
                        "x",
                    ),
                ) if case == "scan" else ()
                self.create_db(items, manifest, issues=issues)
                row = self.csv_rows(
                    self.run_report(
                        f"mismatch-{case}",
                        tolerance_percent=0,
                        tolerance_bytes=10,
                    )
                    / "simple_folder_check.csv"
                )[0]
                self.assertEqual(row["aggregate_size_result"], "WITHIN_TOLERANCE")
                self.assertEqual(row["result"], "NO")
                expected_reason = (
                    "ZERO_SIZE_FILE" if case == "zero" else "SCAN_INCOMPLETE"
                )
                self.assertIn(expected_reason, row["reason"])

    def test_unknown_size_with_storage_only_or_lto_only_path_is_no(self):
        known = storage(str(self.root_a), "Project/known.mov", 100)
        unknown = storage(str(self.root_a), "Project/unknown.mov", 5)
        extra = storage(str(self.root_a), "Project/storage-only.mov", 1)
        self.create_db(
            [known, unknown, extra],
            [lto(known.relative_path, 100, 100), lto(unknown.relative_path, None, None)],
        )
        storage_only = self.csv_rows(
            self.run_report("storage-only", tolerance_percent=0, tolerance_bytes=6)
            / "simple_folder_check.csv"
        )[0]
        self.assertEqual(storage_only["result"], "NO")
        self.assertEqual(storage_only["storage_only_path_count"], "1")

        self.db_path = self.base / "lto-only.sqlite3"
        self.create_db(
            [known, unknown],
            [
                lto(known.relative_path, 100, 100),
                lto(unknown.relative_path, None, None),
                lto("Project/lto-only.mov", 5, 5),
            ],
        )
        lto_only = self.csv_rows(
            self.run_report("lto-only", tolerance_percent=0, tolerance_bytes=5)
            / "simple_folder_check.csv"
        )[0]
        self.assertEqual(lto_only["result"], "NO")
        self.assertEqual(lto_only["lto_only_path_count"], "1")

    def test_unknown_size_with_conflict_zero_or_scan_issue_is_no(self):
        cases = ("conflict", "zero", "scan")
        for case in cases:
            with self.subTest(case=case):
                db_path = self.base / f"{case}.sqlite3"
                self.db_path = db_path
                known_size = 0 if case == "zero" else 100
                known = storage(str(self.root_a), "Project/known.mov", known_size)
                unknown = storage(str(self.root_a), "Project/unknown.mov", 5)
                manifest = [
                    lto(known.relative_path, known_size, known_size),
                    lto(unknown.relative_path, None, None),
                ]
                if case == "conflict":
                    manifest.append(lto(known.relative_path, 200, 200, tape="T2"))
                issues = (
                    (
                        str(self.root_a),
                        str(self.root_a / "Project" / "bad.mov"),
                        "file_stat_error",
                        "x",
                    ),
                ) if case == "scan" else ()
                self.create_db([known, unknown], manifest, issues=issues)
                row = self.csv_rows(
                    self.run_report(case, tolerance_percent=0, tolerance_bytes=5)
                    / "simple_folder_check.csv"
                )[0]
                self.assertEqual(row["result"], "NO")
                counter = {
                    "conflict": "conflicting_lto_path_count",
                    "zero": "zero_size_file_count",
                    "scan": "scan_issue_count",
                }[case]
                self.assertNotEqual(row[counter], "0")

    def test_unknown_size_requires_exact_paths_even_when_counts_and_total_match(self):
        known = storage(str(self.root_a), "Project/storage-known.mov", 100)
        unknown = storage(str(self.root_a), "Project/unknown.mov", 5)
        self.create_db(
            [known, unknown],
            [
                lto("Project/lto-known.mov", 105, 105),
                lto(unknown.relative_path, None, None),
            ],
        )
        row = self.csv_rows(
            self.run_report(tolerance_percent=0, tolerance_bytes=0)
            / "simple_folder_check.csv"
        )[0]
        self.assertEqual(row["storage_file_count"], row["lto_file_count"])
        self.assertEqual(row["aggregate_size_result"], "WITHIN_TOLERANCE")
        self.assertEqual(row["result"], "NO")
        self.assertEqual(row["storage_only_path_count"], "1")
        self.assertEqual(row["lto_only_path_count"], "1")

    def test_unknown_size_outside_aggregate_tolerance_is_no(self):
        known = storage(str(self.root_a), "Project/known.mov", 100)
        unknown = storage(str(self.root_a), "Project/unknown.mov", 50)
        self.create_db(
            [known, unknown],
            [lto(known.relative_path, 100, 100), lto(unknown.relative_path, None, None)],
        )
        row = self.csv_rows(
            self.run_report(tolerance_percent=0, tolerance_bytes=0)
            / "simple_folder_check.csv"
        )[0]
        self.assertEqual(row["result"], "NO")
        self.assertEqual(row["aggregate_size_result"], "OUTSIDE_TOLERANCE")

    def test_190_file_sequence_review_signal_remains_strictly_blocked(self):
        items = [
            storage(str(self.root_a), f"07-11-2024/frame-{index:03d}.dpx", 100)
            for index in range(189)
        ]
        unknown = storage(str(self.root_a), "07-11-2024/frame-189.dpx", 5)
        items.append(unknown)
        manifest = [lto(item.relative_path, 100, 100) for item in items[:-1]]
        manifest.append(lto(unknown.relative_path, None, None))
        self.create_db(items, manifest)
        out_dir = self.run_report(tolerance_percent=0, tolerance_bytes=5)
        row = self.csv_rows(out_dir / "simple_folder_check.csv")[0]
        self.assertEqual(row["known_size_match_count"], "189")
        self.assertEqual(row["unknown_size_path_match_count"], "1")
        self.assertEqual(row["storage_file_count"], "190")
        self.assertEqual(row["lto_file_count"], "190")
        self.assertEqual(row["result"], "YES_WITH_UNKNOWN_SIZE")
        self.assertEqual(self.csv_rows(out_dir / "deletable_folders.csv"), [])
        self.assertEqual(len(self.csv_rows(out_dir / "blocked_folders.csv")), 1)

    def test_simple_check_rejects_size_outside_tolerance(self):
        item = storage(str(self.root_a), "Project/file.mov", 1_000_000)
        self.create_db([item], [lto(item.relative_path, 1_020_000, 1_020_000)])
        row = self.csv_rows(
            self.run_report(tolerance_percent=0.1, tolerance_bytes=10_000)
            / "simple_folder_check.csv"
        )[0]
        self.assertEqual(row["result"], "NO")
        self.assertEqual(row["size_result"], "OUTSIDE_TOLERANCE")
        self.assertIn("SIZE_OUTSIDE_TOLERANCE", row["reason"])

    def test_simple_check_rejects_file_count_mismatch_even_when_size_matches(self):
        items = [
            storage(str(self.root_a), "Project/one.mov", 50),
            storage(str(self.root_a), "Project/two.mov", 50),
        ]
        self.create_db(items, [lto(items[0].relative_path, 100, 100)])
        row = self.csv_rows(
            self.run_report(tolerance_percent=0, tolerance_bytes=0)
            / "simple_folder_check.csv"
        )[0]
        self.assertEqual(row["storage_size_bytes"], "100")
        self.assertEqual(row["lto_size_midpoint_bytes"], "100")
        self.assertEqual(row["size_result"], "WITHIN_TOLERANCE")
        self.assertEqual(row["file_count_result"], "MISMATCH")
        self.assertEqual(row["result"], "NO")
        self.assertIn("FILE_COUNT_MISMATCH", row["reason"])

    def test_simple_check_rejects_missing_lto_folder(self):
        item = storage(str(self.root_a), "Project/file.mov", 100)
        self.create_db([item], [lto("Other/file.mov", 100, 100)])
        row = self.csv_rows(self.run_report() / "simple_folder_check.csv")[0]
        self.assertEqual(row["result"], "NO")
        self.assertEqual(row["lto_size"], "UNKNOWN")
        self.assertEqual(row["size_result"], "UNKNOWN")
        self.assertIn("NO_LTO_FOLDER", row["reason"])

    def test_storage_total_within_summed_rounding_intervals(self):
        items = [
            storage(str(self.root_a), "Project/one.mov", 105),
            storage(str(self.root_a), "Project/two.mov", 205),
        ]
        entries = [
            lto(items[0].relative_path, 100, 110),
            lto(items[1].relative_path, 200, 210),
        ]
        self.create_db(items, entries)
        row = self.csv_rows(self.run_report() / "deletable_folders.csv")[0]
        self.assertEqual(row["storage_total_size_bytes"], "310")
        self.assertEqual(row["lto_total_size_min_bytes"], "300")
        self.assertEqual(row["lto_total_size_max_bytes"], "320")
        self.assertEqual(row["lto_total_size_midpoint_bytes"], "310")
        self.assertEqual(row["storage_minus_lto_min_bytes"], "10")
        self.assertEqual(row["storage_minus_lto_max_bytes"], "-10")
        self.assertEqual(row["folder_size_interval_result"], "WITHIN_INTERVAL")
        self.assertEqual(row["storage_size_within_lto_folder_interval"], "YES")

    def test_storage_total_outside_interval_and_signed_differences(self):
        item = storage(str(self.root_a), "Project/file.mov", 2048)
        self.create_db([item], [lto(item.relative_path, 1024, 1536)])
        row = self.csv_rows(self.run_report() / "blocked_folders.csv")[0]
        self.assertEqual(row["folder_size_interval_result"], "OUTSIDE_INTERVAL")
        self.assertEqual(row["storage_size_within_lto_folder_interval"], "NO")
        self.assertEqual(row["storage_minus_lto_min_human"], "+1.00 KiB")
        self.assertEqual(row["storage_minus_lto_max_human"], "+512 B")
        self.assertEqual(lto_audit.signed_human_bytes(-2 * 1024 * 1024), "-2.00 MiB")
        self.assertEqual(lto_audit.signed_human_bytes(0), "0 B")

    def test_unknown_lto_paths_make_interval_unknown_but_known_bytes_remain(self):
        known = storage(str(self.root_a), "Project/known.mov", 100)
        mixed = storage(str(self.root_a), "Project/mixed.mov", 200)
        unknown = storage(str(self.root_a), "Project/unknown.mov", 300)
        entries = [
            lto(known.relative_path, 100, 100),
            lto(mixed.relative_path, 200, 200),
            lto(mixed.relative_path, None, None, tape="T2"),
            lto(unknown.relative_path, None, None),
        ]
        self.create_db([known, mixed, unknown], entries)
        row = self.csv_rows(self.run_report() / "blocked_folders.csv")[0]
        self.assertEqual(row["lto_matched_path_count"], "3")
        self.assertEqual(row["lto_known_size_file_count"], "2")
        self.assertEqual(row["lto_unknown_size_file_count"], "2")
        self.assertEqual(row["lto_total_size_min_bytes"], "300")
        self.assertEqual(row["lto_total_size_max_bytes"], "300")
        self.assertEqual(row["folder_size_interval_result"], "UNKNOWN_LTO_SIZES")
        self.assertEqual(row["storage_size_within_lto_folder_interval"], "")

    def test_all_lto_sizes_unknown(self):
        items = [
            storage(str(self.root_a), "Project/one.mov", 100),
            storage(str(self.root_a), "Project/two.mov", 200),
        ]
        self.create_db(items, [lto(item.relative_path, None, None) for item in items])
        row = self.csv_rows(self.run_report() / "blocked_folders.csv")[0]
        self.assertEqual(row["lto_known_size_file_count"], "0")
        self.assertEqual(row["lto_unknown_size_file_count"], "2")
        self.assertEqual(row["lto_total_size_min_bytes"], "0")
        self.assertEqual(row["lto_total_size_max_bytes"], "0")
        self.assertEqual(row["folder_size_interval_result"], "UNKNOWN_LTO_SIZES")

    def test_common_file_failures_block_folders(self):
        cases = [
            ("Missing", 100, [], "MISSING_ON_LTO"),
            ("Mismatch", 100, [(200, 200)], "SIZE_MISMATCH"),
            ("Zero", 0, [(0, 0)], "ZERO_SIZE"),
            ("Unknown", 100, [(None, None)], "UNKNOWN_LTO_SIZE"),
        ]
        storage_entries = []
        lto_entries = []
        for folder, size, intervals, _ in cases:
            item = storage(str(self.root_a), f"{folder}/file.mov", size)
            storage_entries.append(item)
            lto_entries.extend(lto(item.relative_path, low, high) for low, high in intervals)
        self.create_db(storage_entries, lto_entries)
        blocked = self.csv_rows(self.run_report() / "blocked_folders.csv")
        reasons = {row["top_folder"]: row["blocking_reasons"] for row in blocked}
        for folder, _, _, reason in cases:
            self.assertIn(reason, reasons[folder])
        rows = {row["top_folder"]: row for row in blocked}
        self.assertEqual(rows["Zero"]["storage_total_size_bytes"], "0")
        self.assertEqual(rows["Missing"]["folder_size_interval_result"], "NO_MATCHED_LTO_PATHS")
        self.assertEqual(rows["Missing"]["manifest_folder_size_interval_result"], "NO_LTO_FOLDER")
        simple_rows = {
            row["folder_name"]: row
            for row in self.csv_rows(self.base / "reports" / "simple_folder_check.csv")
        }
        self.assertEqual(simple_rows["Zero"]["result"], "NO")
        self.assertIn("ZERO_SIZE_FILE", simple_rows["Zero"]["reason"])
        self.assertEqual(simple_rows["Unknown"]["result"], "NO")
        self.assertIn("UNKNOWN_LTO_SIZE", simple_rows["Unknown"]["reason"])

    def test_possible_move_and_ambiguous_move_block_folders(self):
        moved = storage(str(self.root_a), "Moved/new/file.mov", 100)
        ambiguous = storage(str(self.root_a), "Ambiguous/new/file.mov", 100)
        lto_entries = [
            lto("Moved/old/file.mov", 100, 100),
            lto("Ambiguous/old-one/file.mov", 100, 100),
            lto("Ambiguous/old-two/file.mov", 100, 100, tape="T2"),
        ]
        self.create_db([moved, ambiguous], lto_entries)
        blocked = self.csv_rows(self.run_report() / "blocked_folders.csv")
        reasons = {row["top_folder"]: row["blocking_reasons"] for row in blocked}
        self.assertIn("POSSIBLE_MOVED", reasons["Moved"])
        self.assertIn("AMBIGUOUS_MATCH", reasons["Ambiguous"])

    def test_invalid_filesystem_encoding_blocks_folder(self):
        item = storage(str(self.root_a), "Bad/file.mov", 100, valid=0)
        item.relative_path_bytes = b"Bad/file-\xff.mov"
        item.absolute_path_bytes = str(self.root_a).encode() + b"/Bad/file-\xff.mov"
        item.norm_path = lto_audit.raw_norm_key(item.relative_path_bytes)
        self.create_db([item], [lto(item.relative_path, 100, 100)])
        blocked = self.csv_rows(self.run_report() / "blocked_folders.csv")
        self.assertIn("INVALID_FILESYSTEM_ENCODING", blocked[0]["blocking_reasons"])
        simple = self.csv_rows(self.base / "reports" / "simple_folder_check.csv")[0]
        self.assertEqual(simple["result"], "NO")
        self.assertIn("INVALID_FILESYSTEM_ENCODING", simple["reason"])

    def test_identical_lto_tape_copies_are_safe_and_counted(self):
        item = storage(str(self.root_a), "Project/file.mov", 100)
        self.create_db(
            [item],
            [
                lto(item.relative_path, 100, 100),
                lto(item.relative_path, 100, 100),
                lto(item.relative_path, 100, 100, tape="T2"),
            ],
        )
        safe = self.csv_rows(self.run_report() / "deletable_folders.csv")
        self.assertEqual(safe[0]["lto_copy_count"], "3")
        self.assertEqual(safe[0]["lto_tapes"], "T1 | T2")
        self.assertEqual(safe[0]["lto_total_size_min_bytes"], "100")
        self.assertEqual(safe[0]["lto_total_size_max_bytes"], "100")
        self.assertEqual(safe[0]["lto_additional_identical_tape_copy_count"], "1")
        self.assertEqual(safe[0]["lto_folder_manifest_file_count"], "1")
        simple = self.csv_rows(self.base / "reports" / "simple_folder_check.csv")[0]
        self.assertEqual(simple["lto_file_count"], "1")
        self.assertEqual(simple["lto_size_midpoint_bytes"], "100")
        self.assertEqual(simple["result"], "YES")

    def test_conflicting_lto_tape_copies_block(self):
        item = storage(str(self.root_a), "Project/file.mov", 100)
        self.create_db(
            [item],
            [lto(item.relative_path, 100, 100), lto(item.relative_path, 200, 200, tape="T2")],
        )
        blocked = self.csv_rows(self.run_report() / "blocked_folders.csv")
        self.assertIn("CONFLICTING_LTO_ENTRIES", blocked[0]["blocking_reasons"])
        self.assertEqual(blocked[0]["lto_conflicting_size_path_count"], "1")
        self.assertEqual(blocked[0]["lto_total_size_min_bytes"], "0")
        self.assertEqual(blocked[0]["lto_total_size_max_bytes"], "0")
        self.assertEqual(blocked[0]["folder_size_interval_result"], "UNKNOWN_LTO_SIZES")
        self.assertEqual(
            blocked[0]["lto_folder_manifest_conflicting_size_path_count"], "1"
        )
        self.assertEqual(blocked[0]["lto_folder_manifest_total_min_bytes"], "0")
        simple = self.csv_rows(self.base / "reports" / "simple_folder_check.csv")[0]
        self.assertEqual(simple["result"], "NO")
        self.assertIn("CONFLICTING_LTO_ENTRIES", simple["reason"])

    def test_manifest_and_matching_path_totals_expose_extra_paths(self):
        matched = storage(str(self.root_a), "Project/matched.mov", 100)
        storage_only = storage(str(self.root_a), "Project/storage-only.mov", 50)
        entries = [
            lto(matched.relative_path, 100, 100),
            lto("Project/lto-only.mov", 200, 200),
        ]
        self.create_db([matched, storage_only], entries)
        row = self.csv_rows(self.run_report() / "folder_size_comparison.csv")[0]
        self.assertEqual(row["storage_file_count"], "2")
        self.assertEqual(row["storage_total_size_bytes"], "150")
        self.assertEqual(row["lto_matched_path_count"], "1")
        self.assertEqual(row["lto_total_size_min_bytes"], "100")
        self.assertEqual(row["lto_folder_manifest_file_count"], "2")
        self.assertEqual(row["lto_folder_manifest_total_min_bytes"], "300")
        self.assertEqual(row["storage_minus_lto_min_bytes"], "50")
        self.assertEqual(row["storage_minus_lto_manifest_min_bytes"], "-150")
        self.assertEqual(row["manifest_folder_size_interval_result"], "OUTSIDE_INTERVAL")

    def test_compare_report_never_calls_conflicting_lto_a_match(self):
        item = storage(str(self.root_a), "Project/file.mov", 100)
        self.create_db(
            [item],
            [lto(item.relative_path, 100, 100), lto(item.relative_path, 200, 200, tape="T2")],
        )
        out_dir = self.base / "compare"
        result = lto_audit.command_compare(
            argparse.Namespace(
                db=str(self.db_path), out_dir=str(out_dir), no_moved_check=False
            )
        )
        self.assertEqual(result, 0)
        rows = self.csv_rows(out_dir / "all_results.csv")
        self.assertEqual(rows[0]["status"], "CONFLICTING_LTO_ENTRIES")
        self.assertEqual(self.csv_rows(out_dir / "matches.csv"), [])

    def test_identical_storage_paths_on_two_roots_are_independently_safe(self):
        first = storage(str(self.root_a), "Project/file.mov", 100)
        second = storage(str(self.root_b), "Project/file.mov", 100)
        self.create_db([first, second], [lto(first.relative_path, 100, 100)])
        safe = self.csv_rows(self.run_report() / "deletable_folders.csv")
        self.assertEqual(len(safe), 2)
        self.assertEqual({row["source_root"] for row in safe}, {str(self.root_a), str(self.root_b)})
        self.assertTrue(all(row["storage_total_size_bytes"] == "100" for row in safe))
        self.assertTrue(all(row["lto_total_size_min_bytes"] == "100" for row in safe))
        simple = self.csv_rows(self.base / "reports" / "simple_folder_check.csv")
        self.assertEqual(len(simple), 2)
        self.assertEqual({row["source_root"] for row in simple}, {str(self.root_a), str(self.root_b)})

    def test_different_size_storage_duplicates_block_both_roots(self):
        first = storage(str(self.root_a), "Project/file.mov", 100)
        second = storage(str(self.root_b), "Project/file.mov", 200)
        self.create_db([first, second], [lto(first.relative_path, 100, 100)])
        blocked = self.csv_rows(self.run_report() / "blocked_folders.csv")
        self.assertEqual(len(blocked), 2)
        self.assertTrue(
            all("DUPLICATE_ON_STORAGE_DIFFERENT_SIZES" in row["blocking_reasons"] for row in blocked)
        )

    def test_scan_count_mismatch_marks_classification_incomplete(self):
        item = storage(str(self.root_a), "Project/file.mov", 100)
        self.create_db(
            [item],
            [lto(item.relative_path, 100, 100)],
            expected={str(self.root_a): 2},
        )
        blocked = self.csv_rows(self.run_report() / "blocked_folders.csv")
        self.assertIn("INCOMPLETE_CLASSIFICATION", blocked[0]["blocking_reasons"])

    def test_localized_stat_and_directory_errors_block_affected_folders(self):
        safe_item = storage(str(self.root_a), "Safe/file.mov", 100)
        stat_item = storage(str(self.root_a), "StatError/good.mov", 100)
        dir_item = storage(str(self.root_a), "DirError/good.mov", 100)
        items = [safe_item, stat_item, dir_item]
        issues = [
            (str(self.root_a), str(self.root_a / "StatError" / "bad.mov"), "file_stat_error", "x"),
            (str(self.root_a), str(self.root_a / "DirError" / "unreadable"), "directory_read_error", "x"),
        ]
        self.create_db(items, [lto(item.relative_path, 100, 100) for item in items], issues=issues)
        out_dir = self.run_report()
        safe = self.csv_rows(out_dir / "deletable_folders.csv")
        blocked = self.csv_rows(out_dir / "blocked_folders.csv")
        self.assertEqual([row["top_folder"] for row in safe], ["Safe"])
        reasons = {row["top_folder"]: row["blocking_reasons"] for row in blocked}
        self.assertIn("FILE_STAT_ERROR", reasons["StatError"])
        self.assertIn("DIRECTORY_READ_ERROR", reasons["DirError"])
        simple = {
            row["folder_name"]: row
            for row in self.csv_rows(out_dir / "simple_folder_check.csv")
        }
        self.assertEqual(simple["Safe"]["result"], "YES")
        self.assertIn("SCAN_INCOMPLETE", simple["StatError"]["reason"])
        self.assertIn("SCAN_INCOMPLETE", simple["DirError"]["reason"])

    def test_report_order_is_deterministic(self):
        items = [
            storage(str(self.root_a), "z folder/file.mov", 100),
            storage(str(self.root_a), "A folder/file.mov", 100),
            storage(str(self.root_a), "Ю folder/file.mov", 100),
        ]
        self.create_db(items, [lto(item.relative_path, 100, 100) for item in items])
        first = self.run_report("first")
        second = self.run_report("second")
        for filename in (
            "deletable_folders.csv",
            "blocked_folders.csv",
            "folder_size_comparison.csv",
            "simple_folder_check.csv",
            "deletable_folders.txt",
            "deletable_folders.nul",
        ):
            self.assertEqual((first / filename).read_bytes(), (second / filename).read_bytes())

    def test_terminal_table_order_summary_and_duplicate_root_labels(self):
        first = storage(str(self.root_a), "Project/file.mov", 100)
        second = storage(str(self.root_b), "Project/file.mov", 100)
        review_known = storage(str(self.root_a), "Review/known.mov", 100)
        review_unknown = storage(str(self.root_a), "Review/unknown.mov", 5)
        warning = storage(str(self.root_a), "Warning/file.mov", 100)
        unknown = storage(str(self.root_a), "Unknown/file.mov", 200)
        self.create_db(
            [first, second, review_known, review_unknown, warning, unknown],
            [
                lto(first.relative_path, 100, 100),
                lto(review_known.relative_path, 100, 100),
                lto(review_unknown.relative_path, None, None),
                lto(warning.relative_path, 105, 105),
                lto(unknown.relative_path, None, None),
            ],
        )
        self.run_report()
        output = self.last_terminal_output
        self.assertIn("PDF size unit mode: decimal", output)
        self.assertIn("Project [storage-a]", output)
        self.assertIn("Project [storage-b]", output)
        self.assertLess(output.index("Project [storage-a]"), output.index("Project [storage-b]"))
        self.assertIn("YES: 2 folders", output)
        self.assertIn("YES_WITH_UNKNOWN_SIZE: 1 folders", output)
        self.assertIn("YES_WITH_FILE_WARNINGS: 1 folders", output)
        self.assertIn("NO: 1 folders", output)
        self.assertIn("Total storage size YES: 200 B", output)
        self.assertIn("Total storage size YES_WITH_UNKNOWN_SIZE: 105 B", output)
        self.assertIn("Total storage size YES_WITH_FILE_WARNINGS: 100 B", output)
        self.assertIn("Total storage size NO: 200 B", output)

    def test_legacy_database_warns_without_mutating_unit_metadata(self):
        item = storage(str(self.root_a), "Project/file.mov", 100)
        self.create_db(
            [item],
            [lto(item.relative_path, 100, 100)],
            pdf_size_units=None,
        )
        self.run_report()
        self.assertIn("WARNING:", self.last_terminal_output)
        self.assertIn("LEGACY_BINARY_ASSUMED", self.last_terminal_output)
        simple = self.csv_rows(self.base / "reports" / "simple_folder_check.csv")[0]
        self.assertEqual(simple["pdf_size_units"], "LEGACY_BINARY_ASSUMED")
        connection = sqlite3.connect(str(self.db_path))
        try:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'pdf_size_units'"
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNone(row)

    def test_output_path_inside_source_root_is_rejected_before_writing(self):
        item = storage(str(self.root_a), "Project/file.mov", 100)
        self.create_db([item], [lto(item.relative_path, 100, 100)])
        out_dir = self.root_a / "reports"
        with self.assertRaises(RuntimeError):
            lto_audit.command_deletable_folders(
                argparse.Namespace(db=str(self.db_path), out_dir=str(out_dir))
            )
        self.assertFalse(out_dir.exists())


if __name__ == "__main__":
    unittest.main()
