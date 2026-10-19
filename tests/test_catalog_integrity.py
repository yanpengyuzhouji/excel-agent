"""Persist complete catalog JSON and repair legacy truncated metadata."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app.catalog.builder import build_catalog_payload, MAX_TEXT_CHARS
from app.service.service import WorkbookImportService


class CatalogIntegrityTests(unittest.TestCase):
    def test_large_catalog_json_roundtrips_without_losing_tables(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'source.sqlite'
            manifest=[]
            with sqlite3.connect(path) as db:
                for index in range(4):
                    table=f'table_{index}'
                    columns=[{'physical_name':f'c{i}','display_name':'字段说明'*60,
                              'unit_hint':'原始单位'*60,'sql_type':'TEXT'} for i in range(50)]
                    db.execute(f'CREATE TABLE {table} ('+','.join(c['physical_name']+' TEXT' for c in columns)+')')
                    manifest.append({'table_name':table,'sheet':f'工作表{index}','columns':columns})
                db.execute('CREATE TABLE _dbgpt_import_manifest (schema_json TEXT)')
                db.execute('INSERT INTO _dbgpt_import_manifest VALUES (?)',(json.dumps(manifest),))
            # Release the sqlite handle before Windows temporary-directory cleanup.
            db.close()
            built=build_catalog_payload(str(path),workbook_id='w',display_name='book',source_file='book.xlsx',import_id='i',version=1)
            self.assertGreater(len(built['catalog_json']),MAX_TEXT_CHARS)
            self.assertEqual(built['payload'],json.loads(built['catalog_json']))
            self.assertEqual([50]*4,[len(t['columns']) for t in json.loads(built['catalog_json'])['tables']])
            self.assertLessEqual(len(built['catalog_text']),MAX_TEXT_CHARS)
            self.assertEqual(built['schema_digest'],hashlib.sha256(built['catalog_json'].encode()).hexdigest())

    def service_with_corrupt_catalog(self):
        row=SimpleNamespace(workbook_id='w',owner_id='anonymous',active_import_id='i',active_version=1,
                            catalog_json='{"tables":[',display_name='book',source_file='book.xlsx',
                            schema_digest='old',status='ready',gmt_modified=None)
        service=WorkbookImportService.__new__(WorkbookImportService)
        service._dataset_dao=SimpleNamespace(list_by_owner=lambda *a,**k:[SimpleNamespace(workbook_id='w',active_import_id='i')])
        service._catalog_dao=SimpleNamespace(get_by_workbook_id=lambda *a,**k:row,list_by_owner=lambda *a,**k:[row])
        return service,row

    def test_matching_active_version_still_repairs_corrupt_json(self):
        service,row=self.service_with_corrupt_catalog()
        service.refresh_workbook_catalog=Mock(side_effect=lambda _:setattr(row,'catalog_json','{"tables":[{"table_name":"table_1"}]}'))
        result=service.list_workbook_catalog('anonymous')
        service.refresh_workbook_catalog.assert_called_once_with('w')
        self.assertEqual('table_1',result[0]['catalog']['tables'][0]['table_name'])

    def test_unrepairable_catalog_is_not_a_successful_empty_catalog(self):
        service,row=self.service_with_corrupt_catalog()
        service.refresh_workbook_catalog=Mock(side_effect=ValueError('source unavailable'))
        with self.assertRaisesRegex(ValueError,'catalog'):
            service.list_workbook_catalog('anonymous')


if __name__=='__main__': unittest.main()
