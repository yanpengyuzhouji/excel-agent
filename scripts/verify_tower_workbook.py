"""Focused original-workbook HTTP and DeepSeek acceptance, with private artifacts."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path

import httpx
from run_gold_http import check_response, run as run_http
from run_gold_agent import ask


def main():
    root=Path(__file__).resolve().parents[1]/'data/business_acceptance/gold-v1.2'
    gold=json.loads((root/'gold.json').read_text(encoding='utf-8'))
    output=root/'runs'/datetime.now(timezone(timedelta(hours=8))).strftime('tower-%Y%m%d-%H%M%S')
    output.mkdir(parents=True,exist_ok=False)
    base='http://127.0.0.1:8092/api/v1'
    model='deepseek-v4-flash'
    report={'model':model,'gold_sha256':hashlib.sha256((root/'gold.json').read_bytes()).hexdigest(),'questions':[]}
    def save(): (output/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('REPORT '+str(output),flush=True)
    report['http']=run_http(root,base,selected_sources=['83a5'],confirm_warnings=True)
    save()
    source=gold['sources']['83a5']
    parent=None
    with httpx.Client(base_url=base+'/',timeout=180,trust_env=False) as client:
        try:
            parent=check_response(client.post('agent/conversations',json={'title':'Tower regression temporary'}))['conv_id']
            with (root/source['file']).open('rb') as stream:
                item=check_response(client.post(f'agent/conversations/{parent}/attachments',files={'file':(source['filename'],stream)}))
            report['initial_status']=item['status']
            report['errors']=item['errors']
            report['warnings']=sorted({w['code'] for w in item['severe_warnings']})
            if item['errors']: raise ValueError('Unexpected hard import errors')
            item=check_response(client.post(f'agent/conversations/{parent}/attachments/{item["import_id"]}/confirm',
                json={'revision':item['revision'],'accept_severe_warnings':True}))
            if item['status']!='ready':raise ValueError('Warning acceptance did not materialize the workbook')
            imported=check_response(client.get(f'workbook-imports/{item["import_id"]}'))
            report['logical_tables']=[{k:s.get(k) for k in ('name','table_name','region_title','header_row','row_count')}
                                      for s in imported['sheets'] if s.get('enabled') and s.get('kind')=='data']
            table=next(s['table_name'] for s in imported['sheets'] if s['name']=='导线库')
            case=next(c for c in gold['cases'] if c['id']=='G002')
            questions=[q for q in gold['questions'] if q['case_id']=='G002']
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(ask,base,model,q,case,table,item['workbook_id']) for q in questions]
                for future in as_completed(futures):
                    row=future.result();report['questions'].append(row);save();print(row['id']+' '+row['status'],flush=True)
        finally:
            if parent:
                deleted=check_response(client.delete(f'agent/conversations/{parent}'))
                report['source_cleanup_ok']=bool(deleted.get('deleted')) and not deleted.get('cleanup_warnings')
            save()
    report['model_passed']=sum(q['status']=='auto_pass_pending_semantic_review' for q in report['questions'])
    report['model_total']=len(report['questions'])
    report['finished_at']=datetime.now(timezone(timedelta(hours=8))).isoformat()
    save()
    print(json.dumps({'http':report['http']['passed'],'model_passed':report['model_passed'],'model_total':report['model_total']}),flush=True)
    if report['model_passed']!=2 or not report['source_cleanup_ok']:raise SystemExit(1)


if __name__=='__main__':main()
