"""HTTP acceptance using new temporary conversations only; no model calls."""
import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
import time

import httpx

ROOT=Path(__file__).resolve().parents[1]


def quote(name):
    return '"'+name.replace('"','""')+'"'


def check_response(response):
    response.raise_for_status()
    return response.json()


def run(root,base,selected_sources=None,confirm_warnings=False,keep_sources=False,on_prepared=None):
    gold=json.loads((root/'gold.json').read_text(encoding='utf-8'))
    if selected_sources and not set(selected_sources).issubset(gold['sources']):
        raise ValueError('Unknown source ID; refusing an empty or incomplete acceptance run')
    results=[]
    prepared={}
    with httpx.Client(base_url=base.rstrip('/')+'/',timeout=300,trust_env=False) as client:
        check_response(client.get('health'))
        for source_id,source in gold['sources'].items():
            if selected_sources and source_id not in selected_sources: continue
            conv_id=None
            entry={'source':source_id,'status':'failed','checks':[]}
            started=time.monotonic()
            try:
                path=root/source['file']
                if hashlib.sha256(path.read_bytes()).hexdigest()!=source['sha256']: raise ValueError('Source hash changed')
                conv_id=check_response(client.post('agent/conversations',json={'title':'gold-v1.1 HTTP temporary'}))['conv_id']
                with path.open('rb') as stream:
                    attachment=check_response(client.post(f'agent/conversations/{conv_id}/attachments',
                        files={'file':(source['filename'],stream,'application/octet-stream')}))
                entry['attachment_status']=attachment['status']
                if attachment['status']!=source['expected_attachment_status']: raise ValueError('Attachment admission differs')
                entry['checks'].append('whole_file_admission')
                detail=check_response(client.get(f'agent/conversations/{conv_id}'))
                if not any(a['import_id']==attachment['import_id'] for a in detail['scope']['attachments']):
                    raise ValueError('Attachment was not persisted in conversation')
                entry['checks'].append('history_attachment_persistence')
                if attachment['status']=='needs_review' and confirm_warnings:
                    attachment=check_response(client.post(f'agent/conversations/{conv_id}/attachments/{attachment["import_id"]}/confirm',
                        json={'revision':attachment['revision'],'accept_severe_warnings':True}))
                    if attachment['status']!='ready': raise ValueError('Explicit test warning confirmation failed')
                    entry['checks'].append('test_warning_confirmation')
                if attachment['status']=='ready':
                    imported=check_response(client.get(f'workbook-imports/{attachment["import_id"]}'))
                    catalog=next(c for c in check_response(client.get('data-catalog')) if c['workbook_id']==attachment['workbook_id'])
                    tables=catalog['catalog'].get('tables',[])
                    expected_tables={s['table_name'] for s in imported['sheets'] if s.get('enabled') and s.get('kind')=='data'}
                    if {t['table_name'] for t in tables}!=expected_tables:
                        raise ValueError('Agent catalog table coverage differs from imported database')
                    entry['checks'].append('agent_catalog_table_coverage')
                    for case in [c for c in gold['cases'] if c['source']==source_id]:
                        sheets=[s for s in imported['sheets'] if s['name']==case['sheet'] and
                                (case['region'] is None or s.get('region_title')==case['region'])]
                        if len(sheets)!=1: raise ValueError('Logical table selection ambiguous')
                        sheet=sheets[0]
                        selected=[c for c in sheet['columns'] if c.get('enabled',True) and not c.get('derived')]
                        names=[c['source_column'] for c in selected]
                        sql='SELECT _source_row,'+','.join(quote(c['physical_name']) for c in selected)+f' FROM {quote(sheet["table_name"])} ORDER BY _source_row'
                        records={}
                        offset=0
                        # Respect the service's configured result cap; do not treat 500 rows as all data.
                        while True:
                            answer=check_response(client.post(f'workbooks/{attachment["workbook_id"]}/query',
                                json={'sql':sql+f' LIMIT 400 OFFSET {offset}','max_rows':500}))
                            if answer.get('truncated'): raise ValueError('Acceptance page truncated')
                            records.update({r[0]:dict(zip(names,r[1:])) for r in answer['rows']})
                            if len(answer['rows'])<400: break
                            offset+=400
                        actual_rows=[r for r,v in records.items() if v.get(case['key_column']) not in (None,'')]
                        if actual_rows!=case['expected_source_rows']: raise ValueError('HTTP SQL row coverage differs')
                        for coordinate,expected in case['expected_cells'].items():
                            col=coordinate.rstrip('0123456789'); row=int(coordinate[len(col):])
                            actual=records[row][col]; value=expected['value']
                            equal=(actual in (None,'')) if value is None else (actual is not None and Decimal(str(actual))==Decimal(value)) if expected['type']=='n' else str(actual)==value
                            if not equal: raise ValueError('HTTP SQL cell mapping differs: '+coordinate)
                        entry['checks'].append(case['id']+'_sql_rows_and_cells')
                entry['status']='passed'
            except Exception as exc:
                entry['error']=str(exc)[:350]
            finally:
                if conv_id and keep_sources and entry['status']=='passed' and attachment['status']=='ready':
                    prepared[source_id]={'conv_id':conv_id,'attachment':attachment,'imported':imported}
                    if on_prepared: on_prepared(source_id,prepared[source_id])
                elif conv_id:
                    try:
                        deleted=check_response(client.delete(f'agent/conversations/{conv_id}'))
                        if not deleted.get('deleted') or deleted.get('cleanup_warnings'): raise ValueError('Cleanup incomplete')
                        if client.get(f'agent/conversations/{conv_id}').status_code!=404: raise ValueError('Conversation remains')
                        entry['checks'].append('temporary_conversation_deleted')
                    except Exception as exc:
                        entry['status']='failed'; entry['cleanup_error']=str(exc)[:350]
            entry['elapsed_seconds']=round(time.monotonic()-started,3)
            results.append(entry)
            print(json.dumps(entry,ensure_ascii=False),flush=True)
    return {'base_url':base,'gold_sha256':hashlib.sha256((root/'gold.json').read_bytes()).hexdigest(),
            'results':results,'passed':sum(r['status']=='passed' for r in results),'total':len(results),
            'model_calls':0,'confirm_test_warnings':confirm_warnings,'prepared_sources':prepared,
            'scope':'whole-file upload, expected gates, ready-file SQL cell checks, own test cleanup'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT/'data/business_acceptance/gold-v1.2')
    parser.add_argument('--base',default='http://127.0.0.1:8092/api/v1')
    parser.add_argument('--source',action='append',help='Only selected source IDs')
    parser.add_argument('--confirm-test-warnings',action='store_true',help='Explicitly accept warnings in newly created test conversations only')
    args=parser.parse_args()
    result=run(args.root,args.base,args.source,args.confirm_test_warnings)
    name='latest-http-confirmed-results.json' if args.confirm_test_warnings else 'latest-http-results.json'
    (args.root/name).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':result['passed'],'total':result['total']},ensure_ascii=False))
    sys.exit(0 if result['passed']==result['total'] else 1)
