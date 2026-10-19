import sqlite3
import unittest
from app.agent.statistics_tools import profile_sql, compare_totals


class StatisticsToolsTests(unittest.TestCase):
    def test_shares_uses_all_rows_as_denominator_without_deduplication(self):
        from app.agent.multi_workbook import _aggregate_results
        source = {'result_id': 'result-1', 'columns': ['category', 'count'], 'rows': [['A', 64], ['B', 34]]}
        result = _aggregate_results([source], {'operation': 'shares', 'value_column': 'count'})
        self.assertNotIn('error', result)
        from decimal import Decimal, localcontext
        self.assertEqual('98', result['rows'][1][-2])
        with localcontext():
            self.assertEqual(Decimal('34.69'), Decimal(result['rows'][1][-1]).quantize(Decimal('.01')))
        self.assertEqual(['A', 64], result['rows'][0][:-2])
        source['rows'] = [['A', 1], ['A', 1], ['B', 2]]
        result = _aggregate_results([source], {'operation': 'shares', 'value_column': 'count'})
        self.assertEqual(3, result['row_count'])
        self.assertEqual(['25.00', '25.00', '50.0'], [str(Decimal(r[-1])) for r in result['rows']])
        for rows, warning in [([['A', None], ['B', 2]], 'missing_input'), ([['A', 0]], 'zero_denominator')]:
            source['rows'] = rows
            result = _aggregate_results([source], {'operation': 'shares', 'value_column': 'count'})
            self.assertEqual(warning, result['warning'])
            self.assertTrue(all(r[-1] is None for r in result['rows']))
        for changes in ({'truncated': True}, {'rows': [['A', 'bad']]}, {'columns': ['count', 'count']}):
            bad = dict(source, **changes)
            self.assertIn('error', _aggregate_results([bad], {'operation': 'shares', 'value_column': 'count'}))

    def test_decimal_operations_ignore_inherited_inexact_flag_without_clearing_callers(self):
        from decimal import Inexact, localcontext
        from app.agent.multi_workbook import _aggregate_results
        from app.agent.numeric_evidence import calculate_comparison, calculate_weighted_average
        source = {'result_id': 'result-1', 'columns': ['a', 'b'], 'rows': [[1, 2]]}
        with localcontext() as ctx:
            ctx.flags[Inexact] = True
            for action in ({'operation': 'sum', 'value_column': 'a'},
                           {'operation': 'sum', 'operands': [{'result_id': 'result-1', 'row': 0, 'column': 'a'}]},
                           {'operation': 'shares', 'value_column': 'a'}):
                self.assertNotIn('error', _aggregate_results([source], action))
            self.assertEqual('1', compare_totals([source], {'baseline_column': 'a', 'current_column': 'b'})['rows'][0][2])
            self.assertEqual('1', calculate_weighted_average([source], {'value_column': 'a', 'weight_column': 'b'})['rows'][0][-1])
            self.assertEqual('1', calculate_comparison([source], {'operation': 'difference',
                'baseline': {'result_id': 'result-1', 'row': 0, 'column': 'a'},
                'current': {'result_id': 'result-1', 'row': 0, 'column': 'b'}})['rows'][0][0])
            self.assertTrue(ctx.flags[Inexact])

    def test_profile_counts_categories_without_dropping_zero(self):
        catalog = {'catalog': {'tables': [{'table_name': 'data', 'columns': [{'physical_name': 'v'}]}]}}
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE data(v)')
            db.executemany('INSERT INTO data VALUES (?)', [(None,), ('',), ('  ',), (0,), (2022,), (.01,)])
            self.assertEqual([('v', 6, 1, 2, 3)], db.execute(profile_sql('data', ['v'], catalog)).fetchall())
        for table, cols in [('outside', ['v']), ('data', ['secret']), ('data', []), ('data', 'v')]:
            with self.subTest(table=table, columns=cols), self.assertRaises(ValueError):
                profile_sql(table, cols, catalog)

    def test_compare_combines_all_groups_before_calculation(self):
        result = compare_totals([{'result_id': 'result-1', 'columns': ['base', 'now'],
                                 'rows': [['100.1', '120.2'], ['50.2', '80.3']]}],
                                {'baseline_column': 'base', 'current_column': 'now'})
        self.assertEqual(['150.3', '200.5', '50.2'], result['rows'][0][:3])
        self.assertEqual('all cached rows', result['derivation']['inputs'][0]['scope'])

    def test_compare_zero_negative_missing_and_empty_are_distinct(self):
        for rows, expected in [([[0, 50]], ['0', '50', '50', None]),
                               ([[-100, -50]], ['-100', '-50', '50', '-50.0']),
                               ([[None, 50]], [None, '50', None, None]),
                               ([], [None, None, None, None])]:
            with self.subTest(rows=rows):
                result = compare_totals([{'result_id': 'result-1', 'columns': ['a', 'b'], 'rows': rows}],
                                        {'baseline_column': 'a', 'current_column': 'b'})
                self.assertEqual(expected, result['rows'][0])

    def test_compare_rejects_incomplete_ambiguous_and_nonnumeric_inputs(self):
        for result in [{'columns': ['a', 'b'], 'rows': [[1, 2]], 'truncated': True},
                       {'columns': ['a', 'a', 'b'], 'rows': [[1, 1, 2]]},
                       {'columns': ['a', 'b'], 'rows': [['bad', 2]]}]:
            with self.subTest(result=result), self.assertRaises(ValueError):
                compare_totals([dict(result, result_id='result-1')], {'baseline_column': 'a', 'current_column': 'b'})
