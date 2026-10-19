import sqlite3
import unittest

from app.agent.statistics_tools import query_type_advisories


class QueryTypeAdvisoryTests(unittest.TestCase):
    def test_numeric_filter_text_cast_can_silently_drop_equal_values(self):
        catalog = {'catalog': {'tables': [{'table_name': 'data', 'columns': [
            {'physical_name': 'category', 'sql_type': 'REAL'},
            {'physical_name': 'identifier', 'sql_type': 'TEXT'}]}]}}
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE data(category REAL, identifier TEXT, amount REAL)')
            db.executemany('INSERT INTO data VALUES(?,?,?)', [(1, '001', 10), (1.0, '1', 20)])
            sql = "SELECT SUM(amount) FROM data WHERE TRIM(CAST(category AS TEXT))='1'"
            self.assertIsNone(db.execute(sql).fetchone()[0])
            self.assertEqual(30, db.execute('SELECT SUM(amount) FROM data WHERE category=1').fetchone()[0])
        advice = query_type_advisories(sql, catalog)
        self.assertEqual(['category'], advice[0]['columns'])
        self.assertEqual('numeric_text_filter', advice[0]['code'])
        self.assertEqual([], query_type_advisories('SELECT * FROM data WHERE category=1', catalog))
        self.assertEqual([], query_type_advisories("SELECT * FROM data WHERE CAST(identifier AS TEXT)='001'", catalog))
        self.assertEqual([], query_type_advisories('SELECT CAST(category AS TEXT) FROM data', catalog))
        # Blank checks are legitimate, including profile_workbook's own SQL.
        self.assertEqual([], query_type_advisories("SELECT * FROM data WHERE TRIM(CAST(category AS TEXT))=''", catalog))
        self.assertEqual([], query_type_advisories('not valid SQL (', catalog))


if __name__ == '__main__':
    unittest.main()
