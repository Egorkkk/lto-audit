import tempfile
import unittest
from pathlib import Path

import lto_audit as a
from tests.test_lto_audit import lto, write_test_xlsx


class ArchiveCompareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'test.sqlite3'
        self.c = a.connect_db(self.db)
        self.addCleanup(self.c.close)

    def catalog(self, rows):
        book = self.root / 'catalog.xlsx'
        write_test_xlsx(book, [('VU2', [dict(A='Project name', B='Cassette label', C='Path', D='Filename', E='Size')] + rows)])
        a.import_archive_catalog(self.c, book)

    def report(self, entries, report_id=1, issues=()):
        self.c.execute("INSERT INTO lto_reports (id,source_path,source_filename,imported_at,project_header,pdf_size_units) VALUES (?,?,?,'now','OTHER','decimal')", (report_id, str(report_id), str(report_id)))
        a.insert_lto_data(self.c, entries, issues, dict(source_pdf=str(report_id), project='OTHER', expected_files=len(entries), extracted_files=len(entries), difference=0, sequence_files=0, size_unknown_files=0, issues=len(issues)), report_id=report_id)

    def compare(self, folders=('date',)):
        return a.compare_archive_project(self.c, 1, 'VU2', folders)

    def test_copies_unicode_unknown_sizes_and_report_isolation(self):
        self.catalog([dict(A='VU2', B=tape, C='./DATE/cafe\u0301', D='A.MXF') for tape in ('CXZ306L7', 'CXZ307L7', 'CXZ306L7')])
        self.report([lto('date/café/a.mxf', 10, 12, tape=t) for t in ('CXZ306', 'CXZ307')])
        self.report([lto('date/café/a.mxf', 90, 100, tape='CXZ306')], 2)
        summary, rows = self.compare()
        self.assertEqual((summary[0]['expected_unique_files'], summary[0]['found']), (1, 1))
        self.assertEqual(summary[0]['result'], 'COMPLETE')
        self.assertEqual(len(rows), 6)
        self.assertEqual({r['status'] for r in rows}, {'PATH_MATCH_SIZE_UNKNOWN'})

    def test_exact_missing_size_and_cassette_conflicts_and_unexpected_scope(self):
        self.catalog([dict(A='VU2', B='CXZ306L7', C='/date', D=f, E='10') for f in ('a', 'b', 'c', 'd')])
        self.report([lto('date/a', 9, 11, tape='CXZ306'), lto('date/c', 20, 30, tape='CXZ306'), lto('date/d', 10, 10, tape='OTHER'), lto('date/extra', 10, 10, tape='CXZ306'), lto('date2/extra', 10, 10, tape='CXZ306'), lto('date/foreign', 10, 10, tape='OTHER')])
        s, rows = self.compare()
        self.assertEqual([s[0][k] for k in ('found', 'missing', 'conflicts', 'unexpected')], [1, 1, 2, 1])
        self.assertEqual(s[0]['result'], 'CONFLICT')
        self.assertEqual({r['status'] for r in rows}, {'EXACT', 'MISSING_ON_REPORT', 'SIZE_CONFLICT', 'CASSETTE_CONFLICT', 'UNEXPECTED_ON_REPORT'})

    def test_duplicate_conflicts_and_parser_diagnostics(self):
        self.catalog([dict(A='VU2', B='CXZ306L7', C='date', D='a', E=str(n)) for n in (10, 20)])
        self.report([lto('date/a', 10, 10, tape='CXZ306')])
        self.assertEqual(self.compare()[1][0]['status'], 'SIZE_CONFLICT')
        self.c.execute('UPDATE archive_catalog_files SET size_bytes=10')
        self.c.execute("INSERT INTO parse_issues(source_pdf,page,issue_type,details,report_id) VALUES ('1',1,'warning','test',1)")
        s, _ = self.compare()
        self.assertEqual(s[0]['result'], 'CONFLICT')
        self.assertEqual(s[0]['report_parse_issues'], 1)

    def test_missing_folders_project_and_report(self):
        self.catalog([dict(A='VU2', B='CXZ306L7', C='date', D='a')])
        self.report([])
        self.assertEqual(self.compare()[0][0]['result'], 'NOT_FOUND')
        for report, project, folders in ((9, 'VU2', []), (1, 'OTHER', []), (1, 'VU2', ['absent'])):
            with self.assertRaises(ValueError):
                a.compare_archive_project(self.c, report, project, folders)

    def test_cli_multiple_folders_deterministic_csv_readonly(self):
        self.catalog([dict(A='VU2', B='CXZ306L7', C=f, D='a', E='10') for f in ('date', 'source')])
        self.report([lto(f + '/a', 10, 10, tape='CXZ306') for f in ('date', 'source')])
        self.c.close()
        before = self.db.read_bytes()
        for out in ('one', 'two'):
            self.assertEqual(a.main(['compare-archive-project', '--db', str(self.db), '--report', '1', '--archive-project', 'VU2', '--folder', 'source', '--folder', 'date', '--out-dir', str(self.root / out)]), 0)
        self.assertEqual(before, self.db.read_bytes())
        for name in ('archive_folder_summary.csv', 'archive_file_details.csv'):
            self.assertEqual((self.root / 'one' / name).read_bytes(), (self.root / 'two' / name).read_bytes())

    def test_unknown_sequence_and_incompatible_observed_duplicates(self):
        self.catalog([dict(A='VU2', B='CXZ306L7', C='date', D='a', E='10')])
        self.report([lto('date/a', None, None, tape='CXZ306')])
        self.assertEqual(self.compare()[1][0]['status'], 'PATH_MATCH_SIZE_UNKNOWN')
        self.c.execute('UPDATE lto_entries SET size_known=1,size_min_bytes=10,size_max_bytes=10')
        self.report([lto('date/a', 20, 20, tape='CXZ306')], 2)
        self.c.execute('UPDATE lto_entries SET report_id=1')
        self.assertEqual(self.compare()[1][0]['status'], 'SIZE_CONFLICT')
