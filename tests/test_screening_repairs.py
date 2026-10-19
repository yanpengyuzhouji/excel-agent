import importlib.util
from pathlib import Path
import unittest


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ScreeningRepairTests(unittest.TestCase):
    def test_wrong_signed_value_cannot_borrow_magnitude_from_another_task(self):
        check = load('gold_v2_checks').packet_checks
        case = {'metrics': [{'label': '差额', 'value': '49000.6912', 'tolerance': '0.00005'}]}
        packet = {'tasks': [
            {'id': 'bad', 'label': '市区减福清差额', 'operation': 'subtract', 'status': 'ok', 'value': '-49000.6912'},
            {'id': 'abs', 'label': '差额绝对值', 'operation': 'value', 'status': 'ok', 'value': '49000.6912'}]}
        case['metrics'][0]['operation'] = 'subtract'
        self.assertFalse(check(case, packet)['passed'])
        del case['metrics'][0]['operation']
        self.assertFalse(check(case, packet)['passed'])
        packet['tasks'][0]['value'] = '49000.6912'
        self.assertTrue(check(case, packet)['passed'])

    def test_total_rows_do_not_replace_business_record_metric(self):
        check = load('gold_v2_checks').packet_checks
        case = {'metrics': [{'label': '有效记录', 'value': '676', 'tolerance': '0'}]}
        packet = {'tasks': [
            {'id': 'total', 'label': '总行数', 'operation': 'value', 'status': 'ok', 'value': '676'},
            {'id': 'valid', 'label': '有效记录', 'operation': 'value', 'status': 'ok', 'value': '673'}]}
        self.assertFalse(check(case, packet)['passed'])

    def test_packet_metric_must_keep_expected_unit(self):
        check = load('gold_v2_checks').packet_checks
        case = {'metrics': [{'label': '金额', 'value': '10', 'tolerance': '0', 'unit': '万元'}]}
        task = {'id': 'money', 'label': '金额', 'operation': 'value', 'status': 'ok', 'value': '10', 'unit': '元'}
        self.assertFalse(check(case, {'tasks': [task]})['passed'])
        task['unit'] = '万元'
        self.assertTrue(check(case, {'tasks': [task]})['passed'])

    def test_difference_explanation_does_not_become_second_metric(self):
        check = load('numeric_case_checks').scenario_check
        self.assertTrue(check('A08', '差额为50。差额计算使用基期=0。')['passed'])
        self.assertFalse(check('A08', '差额为0。')['passed'])
        self.assertTrue(check('A08', '**差额（本期 - 基期）**：50.00 元')['passed'])

    def test_equal_synonym_is_scoped_and_negation_is_not_accepted(self):
        check = load('gold_v2_checks').text_checks
        case = {'id': 'C19', 'required_text': ['相等']}
        self.assertTrue(check(case, '两项之和等于总数。')['passed'])
        self.assertFalse(check(case, '两项之和不等于总数。')['passed'])
        self.assertFalse(check({'id': 'other', 'required_text': ['相等']}, '等于')['passed'])
