import json
from contextlib import closing
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.agent.multi_workbook import _inspect_source, _internal_metadata_query, run_multi_workbook_agent
from app.service.service import WorkbookImportService


class SheetNotesTests(unittest.TestCase):
    def test_inspection_reads_retained_notes_without_changing_business_rows(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'book.sqlite'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE table_1 (name TEXT)')
                db.execute("INSERT INTO table_1 VALUES ('batch')")
                db.execute('CREATE TABLE _dbgpt_source_lineage (table_name TEXT, source_sheet TEXT, source_row INTEGER, kind TEXT, detail_json TEXT)')
                for table, reason, text in [('table_1', 'note', '备注：采购批次说明'), ('table_1', 'summary', '合计'), ('table_2', 'note', 'private')]:
                    db.execute('INSERT INTO _dbgpt_source_lineage VALUES (?, ?, 101, ?, ?)',
                               (table, '国网批次', 'excluded_row', json.dumps({'reason': reason, 'values': [text, None, text]})))
            service = WorkbookImportService.__new__(WorkbookImportService)
            service._sql_slots = threading.BoundedSemaphore(1)
            service._config = SimpleNamespace(query_row_limit=500)
            snapshot = {'workbook_id': 'a', 'import_id': 'i', 'version': 1, 'database_path': str(path)}
            catalog = {'catalog': {'tables': [{'table_name': 'table_1', 'excluded_row_count': 2, 'columns': []}]}}
            result = _inspect_source(service, snapshot, catalog, ['table_1'])['tables'][0]
            self.assertEqual([['batch']], result['sample_rows'])
            self.assertEqual('备注：采购批次说明', result['notes'][0]['text'])
            self.assertEqual(101, result['notes'][0]['source_row'])
            self.assertEqual(1, len(result['notes']))
            self.assertFalse(result['notes_truncated'])
            self.assertTrue(_inspect_source(service, snapshot, catalog, ['table_1'], notes_offset=-1).get('error'))
            with closing(sqlite3.connect(path)) as db, db:
                for row in range(102, 123):
                    db.execute('INSERT INTO _dbgpt_source_lineage VALUES (?, ?, ?, ?, ?)',
                               ('table_1', '国网批次', row, 'excluded_row', json.dumps({'reason': 'note', 'values': ['说明：' + 'x' * 4100]})))
            first = _inspect_source(service, snapshot, catalog, ['table_1'])['tables'][0]
            self.assertEqual(20, len(first['notes']))
            self.assertTrue(first['notes_truncated'])
            self.assertTrue(first['notes'][1]['text_truncated'])
            last = _inspect_source(service, snapshot, catalog, ['table_1'], notes_offset=first['next_notes_offset'])['tables'][0]
            self.assertEqual(2, len(last['notes']))
            self.assertFalse(last['notes_truncated'])
            self.assertTrue(_inspect_source(service, snapshot, catalog, ['table_2']).get('error'))
            self.assertTrue(_internal_metadata_query('SELECT * FROM _dbgpt_source_lineage'))

    def test_agent_inspection_delivers_notes_without_remark_column(self):
        calls = []
        def query(snapshot, sql, **kwargs):
            calls.append(sql)
            if '_dbgpt_source_lineage' in sql:
                return SimpleNamespace(rows=[['国网批次', 101, json.dumps({'values': ['备注：特种车辆框架采购']})]], truncated=False)
            return SimpleNamespace(columns=['name'], rows=[['batch']])
        requests = []
        def generate(messages):
            requests.append(messages)
            if len(requests) == 1:
                return 'Action: inspect_workbook\nAction Input: {"workbook_id":"a","table_name":"table_1"}'
            self.assertIn('特种车辆框架采购', json.dumps(messages, ensure_ascii=False))
            return 'Action: terminate\nAction Input: {"result":"备注：特种车辆框架采购"}'
        events = list(run_multi_workbook_agent(
            SimpleNamespace(generate_chat=generate),
            SimpleNamespace(active_database_snapshot=lambda *a, **k: {'workbook_id': 'a', 'import_id': 'i', 'version': 1}, execute_readonly_sql_snapshot=query),
            owner_id='owner', question='国网批次sheet的备注',
            catalogs=[{'workbook_id': 'a', 'catalog': {'tables': [{'table_name': 'table_1', 'excluded_row_count': 1, 'columns': []}]}}], max_loops=3))
        inspected = next(e for e in events if e['type'] == 'source.inspect')
        self.assertEqual(101, inspected['tables'][0]['notes'][0]['source_row'])
        self.assertEqual('done', events[-1]['type'])
        self.assertEqual(2, len(calls))


if __name__ == '__main__':
    unittest.main()
