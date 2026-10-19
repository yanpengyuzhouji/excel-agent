"""Run frozen private gold labels against parsing and isolated SQLite materialization."""
import argparse
from contextlib import closing
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def quote(name):
    return '"' + name.replace('"','""') + '"'


def run(root):
    from app.service.parser import parse_workbook
    from app.service.materializer import build_database
    from app.service.service import WorkbookImportService
    from app.service.attachment_policy import review_issues
    gold = json.loads((root/'gold.json').read_text(encoding='utf-8'))
    results, cache, admissions = [], {}, {}
    with tempfile.TemporaryDirectory(prefix='excel-gold-') as temporary:
        for case in gold['cases']:
            started=time.monotonic()
            entry={'id':case['id'],'sheet':case['sheet'],'status':'failed','checks':[]}
            try:
                source=gold['sources'][case['source']]
                path=root/source['file']
                if hashlib.sha256(path.read_bytes()).hexdigest()!=source['sha256']:
                    raise ValueError('Source SHA-256 changed')
                if case['source'] not in cache:
                    cache[case['source']]=parse_workbook(str(path),file_name=source['filename'])
                    validation=WorkbookImportService._validate_draft(cache[case['source']])
                    admissions[case['source']]={'errors':sorted({v['code'] for v in validation['errors']}),
                        'warnings':sorted({v['code'] for v in validation['warnings']})}
                    errors,severe=review_issues(SimpleNamespace(validation=validation,sheets=cache[case['source']]['sheets']))
                    status='blocked' if errors else 'needs_review' if severe else 'ready'
                    admissions[case['source']]['status']=status
                    admissions[case['source']]['expected_status']=source.get('expected_attachment_status')
                original=cache[case['source']]
                matches=[s for s in original['sheets'] if s['name']==case['sheet'] and
                         (case['region'] is None or s.get('region_title')==case['region'])]
                if len(matches)!=1:
                    raise ValueError(f'Expected one logical table, got {len(matches)}')
                sheet=matches[0]
                columns=sheet['columns']
                key=next(i for i,c in enumerate(columns) if c['source_column']==case['key_column'] and not c.get('derived'))
                actual_rows=[r for r in sheet['rows'] if r['values'][key] not in (None,'')]
                actual_indices=[r['source_row'] for r in actual_rows]
                if actual_indices!=case['expected_source_rows']:
                    raise ValueError(f'Source row coverage differs: expected {len(case["expected_source_rows"])}, actual {len(actual_indices)}')
                entry['checks'].append('exact_source_rows')
                by_row={r['source_row']:r for r in actual_rows}
                for coord,expected in case.get('expected_cells',{}).items():
                    column=coord.rstrip('0123456789')
                    number=int(coord[len(column):])
                    index=next(i for i,c in enumerate(columns) if c['source_column']==column and not c.get('derived'))
                    actual=by_row[number]['values'][index]
                    raw=expected['value']
                    if raw is None:
                        equal=actual in (None,'')
                    elif expected['type']=='n':
                        equal=actual is not None and Decimal(str(actual))==Decimal(raw)
                    else:
                        equal=str(actual)==raw
                    if not equal: raise ValueError('Source cell mapping differs at '+coord)
                if case.get('expected_cells'): entry['checks'].append('exact_cell_values')
                for aggregate in case['aggregates']:
                    index=next(i for i,c in enumerate(columns) if c['source_column']==aggregate['source_column'] and not c.get('derived'))
                    values=[Decimal(str(r['values'][index])) for r in actual_rows if r['values'][index] not in (None,'')]
                    if len(values)!=aggregate['numeric_cells'] or abs(sum(values,Decimal(0))-Decimal(aggregate['expected']))>Decimal(aggregate['tolerance']):
                        raise ValueError('Parsed numeric aggregate differs from frozen XML oracle')
                    entry['checks'].append('parse_sum_'+aggregate['source_column'])
                # Isolate the annotated worksheet; whole-workbook admission is reported separately.
                draft=deepcopy(original)
                draft['sheets']=[s for s in draft['sheets'] if s['name']==case['sheet']]
                if draft.get('source_layouts'):
                    draft['source_layouts']=[s for s in draft['source_layouts'] if s.get('name',s.get('sheet_name'))==case['sheet']]
                database=Path(temporary)/(case['id']+'.sqlite')
                build_database(draft,str(database),import_id=case['id'],workbook_id='gold',version=1,
                               file_name=source['filename'],source_hash=source['sha256'])
                table=quote(sheet['table_name'])
                with closing(sqlite3.connect(database)) as db:
                    rows=case['expected_source_rows']
                    where='_source_row IN ('+','.join('?' for _ in rows)+')'
                    count=db.execute(f'SELECT COUNT(*) FROM {table} WHERE {where}',rows).fetchone()[0]
                    if count!=case['expected_count']: raise ValueError('SQLite count differs')
                    entry['checks'].append('sqlite_count')
                    for aggregate in case['aggregates']:
                        col=next(c for c in columns if c['source_column']==aggregate['source_column'] and not c.get('derived'))
                        total=db.execute(f'SELECT SUM({quote(col["physical_name"])}) FROM {table} WHERE {where}',rows).fetchone()[0]
                        if total is None or abs(Decimal(str(total))-Decimal(aggregate['expected']))>Decimal(aggregate['tolerance']):
                            raise ValueError('SQLite sum differs')
                        entry['checks'].append('sqlite_sum_'+aggregate['source_column'])
                entry['status']='passed'
            except Exception as exc:
                entry['error']=str(exc)[:350]
            entry['elapsed_seconds']=round(time.monotonic()-started,3)
            results.append(entry)
        for case in gold['negative_cases']:
            entry={'id':case['id'],'status':'failed'}
            try:
                path=root/case['file']
                if hashlib.sha256(path.read_bytes()).hexdigest()!=case['sha256']:
                    raise RuntimeError('Source SHA-256 changed')
                parse_workbook(str(path))
                entry['error']='Unsupported format unexpectedly accepted'
            except ValueError as exc:
                # A format rejection must be explicit, not an arbitrary parse failure.
                if 'support' in str(exc).lower() or '格式' in str(exc): entry['status']='passed'
                else: entry['error']=str(exc)[:350]
            except Exception as exc: entry['error']=str(exc)[:350]
            results.append(entry)
    return {'gold_sha256':hashlib.sha256((root/'gold.json').read_bytes()).hexdigest(),
            'scope':'annotated worksheet parsing + temporary SQLite; not full-file import or LLM answer accuracy',
            'review_status':gold['review_status'],'results':results,'whole_workbook_admission':admissions,
            'passed':sum(r['status']=='passed' for r in results),'total':len(results),
            'admission_expectations_passed':all(v['expected_status'] is None or v['status']==v['expected_status'] for v in admissions.values()),
            'agent_questions':len(gold['questions']),'agent_execution':'not_run'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT/'data/business_acceptance/gold-v1.2')
    args=parser.parse_args()
    result=run(args.root)
    (args.root/'latest-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    sys.exit(0 if result['passed']==result['total'] and result['admission_expectations_passed'] else 1)
