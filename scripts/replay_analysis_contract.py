"""Offline compiler replay of captured SQL evidence, NOT a live model accuracy run.

Plans here are explicit test fixtures. Their correctness does not prove that a
model can produce the same plans. No API, user database or credentials are used.
"""
import argparse
import json
from pathlib import Path
import sys
from types import SimpleNamespace as N

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.agent.analysis_result import AnalysisResult


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--pdf', action='store_true')
    args = parser.parse_args()
    report = json.loads((args.run/'acceptance.json').read_text(encoding='utf-8'))
    sheets = report['prepared_sources']['dcb9']['imported']['sheets']
    args.output.mkdir(parents=True, exist_ok=True)
    reviewed = []
    for case_id in ('C01', 'C03', 'C22'):
        events = json.loads((args.run/case_id/'events.json').read_text(encoding='utf-8'))
        evidence = [e for e in events if e['type'] == 'source.query.result']
        book = evidence[0]['source']['workbook_id']
        catalogs = [{'workbook_id': book, 'display_name': '配网月报（历史离线证据）',
                     'catalog': {'tables': [dict(s, display_name=s['name']) for s in sheets]}}]
        def source(table, column=None):
            return {'workbook_id': book, 'table_name': table, 'column': column}
        def binding(result_id, column, row=None):
            return dict(result_id=result_id, column=column, **({'row': row} if row is not None else {}))
        if case_id == 'C01':
            tasks = [
                {'id': 'city', 'label': '市区投资', 'operation': 'value', 'source': source('table_1', 'column_h'), 'decimal_places': 4},
                {'id': 'fuqing', 'label': '福清投资', 'operation': 'value', 'source': source('table_7', 'column_h'), 'decimal_places': 4},
                {'id': 'delta', 'label': '市区减福清差额', 'operation': 'subtract', 'inputs': ['city', 'fuqing'], 'decimal_places': 4},
                {'id': 'ratio', 'label': '市区/福清比值', 'operation': 'ratio', 'inputs': ['city', 'fuqing'], 'decimal_places': 4}]
            bindings = {'city': binding('result-1', 'total', 0), 'fuqing': binding('result-2', 'total', 0)}
            expected = ['85347.4729', '36346.7817', '49000.6912', '2.3481']
        elif case_id == 'C03':
            tasks = [
                {'id': 'count', 'label': 'E列非空记录总数', 'operation': 'sum', 'unit': '条',
                 'sources': [source('table_'+str(i)) for i in range(1, 14)]},
                {'id': 'money', 'label': 'H列投资总额', 'operation': 'sum', 'decimal_places': 4,
                 'sources': [source('table_'+str(i), 'column_h') for i in range(1, 14)]}]
            bindings = {'count': binding('result-1', 'cnt'), 'money': binding('result-1', 'total')}
            expected = ['2202', '225997.5736']
        else:
            tasks = [
                {'id': 'base', 'label': '2021年计划投资', 'operation': 'sum', 'decimal_places': 4,
                 'sources': [source('table_'+str(i), 'column_h') for i in range(1, 14)]},
                {'id': 'now', 'label': '2022年计划投资', 'operation': 'sum', 'decimal_places': 4,
                 'sources': [source('table_'+str(i), 'column_h') for i in range(1, 14)]},
                {'id': 'delta', 'label': '2022减2021差额', 'operation': 'subtract', 'inputs': ['now', 'base'], 'decimal_places': 4},
                {'id': 'growth', 'label': '增长率', 'operation': 'growth_rate', 'inputs': ['base', 'now'], 'decimal_places': 2}]
            bindings = {'base': binding('result-1', 'inv_2021'), 'now': binding('result-1', 'inv_2022')}
            expected = ['3919.5213', '130794.6680', '126875.1467', '3237.01']
        plan = AnalysisResult({'tasks': tasks}, catalogs)
        packet = plan.resolve(bindings, evidence)
        actual = [t['value'] for t in packet['tasks']]
        assert actual == expected, (case_id, actual, expected)
        folder = args.output/case_id
        folder.mkdir(exist_ok=True)
        document = folder/'report.html'
        document.write_text(plan.html(packet, case_id+' 离线分析结果'), encoding='utf-8')
        (folder/'answer.md').write_text(plan.text(packet), encoding='utf-8')
        (folder/'analysis.json').write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding='utf-8')
        if args.pdf:
            from app.api.endpoints import _render_report_pdf
            pdf = _render_report_pdf(str(document), N(config=N(pdf_font_path='', resolved_storage_path=lambda: str(args.output))))
            (folder/'report.pdf').write_bytes(pdf.body)
        reviewed.append({'id': case_id, 'passed': True, 'values': actual})
    summary = {'scope': 'offline_cached_evidence_with_explicit_fixture_plans_not_model_accuracy', 'cases': reviewed}
    (args.output/'replay.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
