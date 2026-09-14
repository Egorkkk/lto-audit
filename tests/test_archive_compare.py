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
        self.assertEqual(s[0]['presence_status'], 'COMPLETE')
        self.assertEqual(s[0]['size_status'], 'VERIFIED')
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

    def test_real_vu2_date_path_and_tape_pass_without_prefix_rewriting(self):
        directory = '20240129/CAM_A/A_0021_1D1D/A_0021_1D1D'
        filename = 'A_0021C001_240129_181231_p1D1D.mxf'
        self.catalog([dict(A='VU2', B='FF7472L7', C='/' + directory, D=filename)])
        self.report([
            lto(directory + '/' + filename, 100, 110, tape='FF7472'),
            lto('PROXY/20240129/' + filename.replace('.mxf', '.mov'), 10, 20, tape='FF7669'),
        ])
        summaries, details = self.compare(['20240129'])
        self.assertEqual(
            [summaries[0][k] for k in ('expected_unique_files', 'found', 'missing', 'conflicts', 'unexpected')],
            [1, 1, 0, 0, 0],
        )
        self.assertEqual(details[0]['status'], 'PATH_MATCH_SIZE_UNKNOWN')
        self.assertEqual(details[0]['yoyotta_cassette'], 'FF7472')

    def test_real_vu2_mojibake_path_is_not_repaired_by_filename(self):
        filename = '20240905_174421.mp4'
        self.catalog([dict(A='VU2', B='FF2018L7', C='/20240905/PHONE Ð—Ð°Ñ\x8fÑ†', D=filename)])
        self.report([
            lto('20240905/PHONE Заяц/' + filename, 100, 110, tape='FF2018'),
            lto('20240905/PHONE ????/' + filename, 100, 110, tape='FF2018'),
        ])
        summaries, details = self.compare(['20240905'])
        self.assertEqual(
            [summaries[0][k] for k in ('expected_unique_files', 'found', 'missing', 'conflicts', 'unexpected')],
            [1, 0, 1, 0, 2],
        )
        self.assertEqual(details[0]['status'], 'MISSING_ON_REPORT')

    def test_presence_and_size_are_independent_of_global_diagnostics(self):
        cases = {
            'unknown': ([''], [10], [], 'COMPLETE', 'UNKNOWN'),
            'partial': (['10', ''], [10, 10], [], 'COMPLETE', 'PARTIAL'),
            'verified': (['10'], [10], [], 'COMPLETE', 'VERIFIED'),
            'missing': (['10', '10'], [10], [], 'INCOMPLETE', 'VERIFIED'),
            'unexpected': (['10'], [10], ['extra'], 'INCOMPLETE', 'VERIFIED'),
            'only_unexpected': (['10'], [], ['extra'], 'INCOMPLETE', 'UNKNOWN'),
            'absent': (['10'], [], [], 'NOT_FOUND', 'UNKNOWN'),
            'mismatch': (['10'], [20], [], 'CONFLICT', 'CONFLICT'),
        }
        rows, entries = [], []
        for folder, (sizes, observed, extras, _, _) in cases.items():
            rows.extend(dict(A='VU2', B='FF7472L7', C=folder, D=str(i), E=size)
                        for i, size in enumerate(sizes))
            entries.extend(lto(f'{folder}/{i}', size, size, tape='FF7472')
                           for i, size in enumerate(observed))
            entries.extend(lto(f'{folder}/{name}', 10, 10, tape='FF7472') for name in extras)
        self.catalog(rows)
        self.report(entries)
        before, details_before = self.compare([])
        self.c.execute("INSERT INTO parse_issues(source_pdf,page,issue_type,details,report_id) VALUES ('1',1,'sequence_file_size_unknown','OTHER_PROJECT/path',1)")
        self.c.execute('UPDATE report_stats SET difference=1 WHERE report_id=1')
        after, details_after = self.compare([])
        self.assertEqual(details_before, details_after)
        self.assertEqual([r['presence_status'] for r in before], [r['presence_status'] for r in after])
        for summary in after:
            with self.subTest(folder=summary['folder']):
                case = cases[summary['folder']]
                self.assertEqual((summary['presence_status'], summary['size_status']), case[-2:])
                self.assertEqual(summary['result'], summary['presence_status'])
                self.assertEqual(summary['expected'], summary['expected_unique_files'])
                self.assertEqual(summary['archive_project'], summary['project'])
                self.assertEqual(summary['report_parse_issues'], 1)
                self.assertEqual(summary['report_count_mismatches'], 1)
                self.assertIn('global informational', summary['notes'])

    def test_projects_share_database_and_unknown_project_lists_available_names(self):
        self.catalog([
            dict(A='VU2', B='FF7472L7', C='date', D='vu2.mxf'),
            dict(A='KBD2', B='FF2003L7', C='date', D='kbd2.mxf', E='10'),
        ])
        self.report([lto('date/vu2.mxf', 10, 10, tape='FF7472'),
                     lto('date/kbd2.mxf', 10, 10, tape='FF2003')])
        for project, filename, size_status in [('VU2', 'vu2.mxf', 'UNKNOWN'), ('KBD2', 'kbd2.mxf', 'VERIFIED')]:
            summary, details = a.compare_archive_project(self.c, 1, project, [])
            self.assertEqual(summary[0]['expected'], 1)
            self.assertEqual(summary[0]['presence_status'], 'COMPLETE')
            self.assertEqual(summary[0]['size_status'], size_status)
            self.assertEqual([r['filename'] for r in details], [filename])
            self.assertEqual({r['archive_project'] for r in details}, {project})
        with self.assertRaisesRegex(ValueError, 'No imported archive project: VU3.*Available projects: KBD2, VU2'):
            a.compare_archive_project(self.c, 1, 'VU3', [])
