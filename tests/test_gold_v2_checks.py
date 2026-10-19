"""Synthetic screening checks; no private acceptance data enters this file."""
import importlib.util
from pathlib import Path
from decimal import Decimal

spec=importlib.util.spec_from_file_location('gold_v2_checks',Path(__file__).resolve().parents[1]/'scripts/gold_v2_checks.py')
checks=importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)

def test_numeric_chinese_and_grouping():
    assert checks.numbers('合计50,571个，差额−12.50万元')==[Decimal('50571'),Decimal('-12.50')]

def test_space_grouping_and_full_width_colon():
    assert checks.numbers('拉力77 090 N、75\u202f190 N')==[Decimal('77090'),Decimal('75190')]
    assert checks.numbers('两行77\n090')==[Decimal('77'),Decimal('90')]
    assert checks.compact('型号： A')==checks.compact('型号:A')

def test_refusal_requires_actual_refusal():
    case={'kind':'refusal','refusal_terms':['无法','缺少']}
    assert not checks.text_checks(case,'同比为20%')['passed']
    assert checks.text_checks(case,'缺少回款数据，无法计算')['passed']

def test_metric_tolerance():
    case={'metrics':[{'label':'amount','value':'1.2345','tolerance':'0.00005'}]}
    assert checks.text_checks(case,'金额1.23451元')['passed']
    assert not checks.text_checks(case,'金额1.235元')['passed']

def test_pairs_reject_swapped_mapping():
    case={'expected_pairs':[['100','1*20'],['200','1*40']]}
    assert checks.text_checks(case,'100: 1*20\n200: 1*40')['passed']
    assert not checks.text_checks(case,'100: 1*40\n200: 1*20')['passed']

def test_ranking_order():
    case={'required_order':['Alpha','Beta','Gamma']}
    assert checks.text_checks(case,'Alpha > Beta > Gamma')['passed']
    assert not checks.text_checks(case,'Beta > Alpha > Gamma')['passed']

def test_html_ignores_style_script_data():
    text,table=checks.html_text('<style>999</style><script>888</script><table><tr><td>17</td></tr></table>')
    assert table and checks.numbers(text)==[Decimal('17')]
