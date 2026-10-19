"""Exact output-expression execution must not change SQL filtering/grouping/sorting."""
import sqlite3
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from app.service.decimal_projection import compile_decimal_projection, register_decimal_functions


class DecimalProjectionTests(unittest.TestCase):
    def test_source_row_order_keeps_decimal_output_and_original_order(self):
        sql = 'SELECT scenario, ROUND(CAST(now AS TEXT)-CAST(base AS TEXT),2) AS diff FROM data ORDER BY rowid'
        rows, columns = self.run_sql(sql)
        self.assertEqual(['diff'], columns)
        self.assertEqual([('round', '0.01'), ('large', '0.01'), ('null', None), ('zero', '0.00')], rows)
        for order in ('diff', '2', 'ROUND(now-base,2)'):
            original = 'SELECT scenario, ROUND(now-base,2) AS diff FROM data ORDER BY '+order
            self.assertEqual((original, []), compile_decimal_projection(original))

    def test_real_readonly_service_opts_in_and_preserves_database(self):
        from test_resource_budgets import service_for
        with TemporaryDirectory() as folder:
            path = Path(folder)/'book.sqlite'
            saved = sqlite3.connect(path)
            try:
                self.connection.backup(saved)
            finally:
                saved.close()
            before = path.read_bytes()
            service = service_for(str(path))
            sql = "SELECT ROUND(now-base,2) AS diff FROM data WHERE scenario='round'"
            result = service.execute_readonly_sql_snapshot({'database_path': str(path)}, sql, exact_output=True)
            self.assertEqual([['0.01']], result.rows)
            self.assertEqual(sql, result.sql)
            self.assertEqual(['diff'], result.decimal_columns)
            self.assertIn('_excel_dec_round', result.execution_sql.lower())
            self.assertEqual(before, path.read_bytes())
            with self.assertRaises(ValueError):
                service.execute_readonly_sql_snapshot({'database_path': str(path)}, 'DELETE FROM data', exact_output=True)

    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.execute('CREATE TABLE data (scenario TEXT, base REAL, now REAL, id TEXT)')
        self.connection.executemany('INSERT INTO data VALUES (?,?,?,?)', [
            ('round', 1, 1.005, '00001'), ('large', 1000000000000.01, 1000000000000.02, '00002'),
            ('null', None, 50, '00003'), ('zero', 0, 0, '00004')])
        self.connection.commit()
        register_decimal_functions(self.connection)

    def tearDown(self):
        self.connection.close()

    def run_sql(self, sql):
        compiled, columns = compile_decimal_projection(sql)
        return self.connection.execute(compiled).fetchall(), columns

    def test_rounding_after_subtraction_is_decimal_not_float(self):
        rows, columns = self.run_sql("SELECT ROUND(now-base,2) AS diff FROM data WHERE scenario='round'")
        self.assertEqual([('0.01',)], rows)
        self.assertEqual(['diff'], columns)

    def test_large_difference_and_cast_preserve_decimal_precision(self):
        rows, columns = self.run_sql("SELECT CAST(ROUND(now-base,12) AS TEXT) AS diff FROM data WHERE scenario='large'")
        self.assertEqual([('0.010000000000',)], rows)
        self.assertEqual(['diff'], columns)

    def test_raw_identifiers_and_original_rows_are_unchanged(self):
        rows, columns = self.run_sql("SELECT id, now FROM data WHERE scenario='round'")
        self.assertEqual([('00001', 1.005)], rows)
        self.assertEqual([], columns)

    def test_order_group_distinct_cte_and_alias_predicates_are_not_reinterpreted(self):
        queries = [
            'SELECT now-base AS diff FROM data ORDER BY diff',
            'SELECT scenario, SUM(now-base) AS diff FROM data GROUP BY scenario',
            'SELECT DISTINCT now-base AS diff FROM data',
            'WITH c AS (SELECT * FROM data) SELECT now-base AS diff FROM c',
            'SELECT now-base AS diff FROM data WHERE diff>0',
            'SELECT now-base AS diff FROM data WHERE DIFF>0',
            'SELECT a.now-a.base AS diff FROM data a JOIN data b ON DIFF>0',
            'SELECT CAST(now-base AS REAL) AS diff FROM data']
        for sql in queries:
            with self.subTest(sql=sql):
                compiled, columns = compile_decimal_projection(sql)
                self.assertEqual(sql, compiled)
                self.assertEqual([], columns)

    def test_null_and_division_by_zero_are_not_fabricated_zero(self):
        rows, _ = self.run_sql("SELECT now-base AS diff, now/base AS ratio FROM data WHERE scenario='null'")
        self.assertEqual([(None, None)], rows)
        rows, _ = self.run_sql("SELECT now/base AS ratio FROM data WHERE scenario='zero'")
        self.assertEqual([(None,)], rows)

    def test_no_implicit_name_changes_or_multi_statement_recovery(self):
        for sql in ['SELECT now-base FROM data', 'SELECT now-base AS d FROM data; SELECT 1']:
            self.assertEqual((sql, []), compile_decimal_projection(sql))

    def test_integer_division_keeps_native_sqlite_semantics(self):
        rows, columns = self.run_sql('SELECT 3/2 AS quotient')
        self.assertEqual([(1,)], rows)
        self.assertEqual([], columns)
