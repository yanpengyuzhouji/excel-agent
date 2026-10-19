"""Private model acceptance. Requires authorization to send business data to the model."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time

import httpx
from run_gold_http import check_response, run as run_http

ROOT=Path(__file__).resolve().parents[1]


def numeric(value):
    try: return Decimal(str(value).replace(',',''))
    except (InvalidOperation,ValueError): return None


def grade(question,case,table,events):
    done=next((e for e in reversed(events) if e.get('type')=='done'),None)
    error=next((e for e in reversed(events) if e.get('type')=='error'),None)
    answer=done.get('full_answer','') if done else ''
    expected=Decimal(str(question['expected'].get('count',question['expected'].get('expected'))))
    tolerance=Decimal(question['expected'].get('tolerance','0'))
    evidence=False
    queries=[]
    for event in events:
        if event.get('type')!='source.query.result': continue
        sql=event.get('sql','')
        queries.append(sql)
        tables=re.findall(r'\b(?:FROM|JOIN)\s+(?:"([^"]+)"|`([^`]+)`|\[([^]]+)\]|([^\s;,()]+))',sql,re.I)
        referenced={next(x for x in match if x) for match in tables}
        if referenced!={table}: continue
        for row in event.get('rows',[]):
            for value in row:
                number=numeric(value)
                if number is not None and abs(number-expected)<=tolerance:
                    evidence=True
    numbers=[numeric(v) for v in re.findall(r'(?<![\w.])[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?',answer)]
    answer_match=any(n is not None and abs(n-expected)<=tolerance for n in numbers)
    if error or not done: status='execution_error'
    elif done.get('partial') or done.get('truncated'): status='incomplete'
    elif evidence and answer_match: status='auto_pass_pending_semantic_review'
    else: status='needs_review'
    return {'status':status,'expected':str(expected),'evidence_numeric_and_table_match':evidence,
            'answer_numeric_match':answer_match,'answer':answer,'queries':queries,
            'error':error.get('error') if error else None,'done':done,
            'grading_limit':'Numeric and table-scope screening only; unit, contradictions and unsupported claims require semantic review.'}


def ask(base,model,question,case,table,workbook_id):
    events=[]; conv_id=None; started=time.monotonic(); cleanup=None
    try:
        with httpx.Client(base_url=base.rstrip('/')+'/',timeout=360,trust_env=False) as client:
            with client.stream('POST','agent/ask',json={'question':question['question'],'model':model,
                'scope':{'mode':'selected','workbook_ids':[workbook_id]},'max_rows':500}) as response:
                response.raise_for_status()
                data=[]
                for line in response.iter_lines():
                    if line.startswith('data:'): data.append(line[5:].lstrip())
                    elif not line and data:
                        event=json.loads('\n'.join(data)); data=[]
                        events.append(event)
                        if event.get('type')=='session': conv_id=event['conv_id']
                        if event.get('type') in ('done','error'): break
            result=grade(question,case,table,events)
    except Exception as exc:
        result={'status':'execution_error','error':str(exc)[:500]}
    finally:
        if conv_id:
            try:
                with httpx.Client(base_url=base.rstrip('/')+'/',timeout=60,trust_env=False) as client:
                    deleted=check_response(client.delete(f'agent/conversations/{conv_id}'))
                    if not deleted.get('deleted') or deleted.get('cleanup_warnings'): raise ValueError('Cleanup warning')
                    if client.get(f'agent/conversations/{conv_id}').status_code!=404: raise ValueError('Conversation remains')
            except Exception as exc: cleanup=str(exc)[:350]
    return {'id':question['id'],'case_id':case['id'],'question':question['question'],
            'model':model,'elapsed_seconds':round(time.monotonic()-started,3),
            'cleanup_error':cleanup,'events':events,**result}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT/'data/business_acceptance/gold-v1.2')
    parser.add_argument('--base',default='http://127.0.0.1:8092/api/v1')
    parser.add_argument('--model',default='deepseek-v4-flash')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    now=datetime.now(timezone(timedelta(hours=8)))
    prefix=re.sub(r'[^a-zA-Z0-9_-]','_',args.model)
    output=args.output or args.root/'runs'/(prefix+'-'+now.strftime('%Y%m%d-%H%M%S'))
    output.mkdir(parents=True,exist_ok=False)
    gold=json.loads((args.root/'gold.json').read_text(encoding='utf-8'))
    report={'started_at':now.isoformat(),'base_url':args.base,'model':args.model,
        'gold_sha256':hashlib.sha256((args.root/'gold.json').read_bytes()).hexdigest(),
        'questions':[],'source_uploads':[],'cleanup_errors':[],'model_questions_total':len(gold['questions'])}
    def save():
        (output/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    print('REPORT '+str(output),flush=True)
    report['http_regression']=run_http(args.root,args.base,confirm_warnings=True)
    save()
    print('HTTP '+str(report['http_regression']['passed'])+'/'+str(report['http_regression']['total']),flush=True)
    with httpx.Client(base_url=args.base.rstrip('/')+'/',timeout=300,trust_env=False) as client:
        models=check_response(client.get('models'))
        if not any(m['name']==args.model for m in models): raise ValueError('Requested model not configured')
        for source_id,source in gold['sources'].items():
            parent=None
            questions=[q for q in gold['questions'] if next(c for c in gold['cases'] if c['id']==q['case_id'])['source']==source_id]
            try:
                path=args.root/source['file']
                if hashlib.sha256(path.read_bytes()).hexdigest()!=source['sha256']: raise ValueError('Source changed')
                parent=check_response(client.post('agent/conversations',json={'title':'Gold temporary source'}))['conv_id']
                with path.open('rb') as stream:
                    item=check_response(client.post(f'agent/conversations/{parent}/attachments',files={'file':(source['filename'],stream)}))
                initial=item['status']
                if initial=='needs_review':
                    item=check_response(client.post(f'agent/conversations/{parent}/attachments/{item["import_id"]}/confirm',
                        json={'revision':item['revision'],'accept_severe_warnings':True}))
                report['source_uploads'].append({'source':source_id,'initial_status':initial,'status':item['status']})
                if item['status']!='ready':
                    report['questions'].extend({'id':q['id'],'case_id':q['case_id'],'status':'blocked_by_import',
                                               'source_status':item['status']} for q in questions)
                    save(); print(source_id+' blocked '+str(len(questions)),flush=True);continue
                imported=check_response(client.get(f'workbook-imports/{item["import_id"]}'))
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures={}
                    for question in questions:
                        case=next(c for c in gold['cases'] if c['id']==question['case_id'])
                        tables=[s for s in imported['sheets'] if s['name']==case['sheet'] and
                                (case['region'] is None or s.get('region_title')==case['region'])]
                        if len(tables)!=1: raise ValueError('Ambiguous question source')
                        future=pool.submit(ask,args.base,args.model,question,case,tables[0]['table_name'],item['workbook_id'])
                        futures[future]=question['id']
                    for future in as_completed(futures):
                        result=future.result();report['questions'].append(result);save()
                        print(result['id']+' '+result['status']+' '+str(result.get('elapsed_seconds'))+'s',flush=True)
            except Exception as exc:
                done_ids={q['id'] for q in report['questions']}
                report['questions'].extend({'id':q['id'],'case_id':q['case_id'],'status':'source_setup_error','error':str(exc)[:500]}
                                           for q in questions if q['id'] not in done_ids)
            finally:
                if parent:
                    try:
                        deleted=check_response(client.delete(f'agent/conversations/{parent}'))
                        if deleted.get('cleanup_warnings'): raise ValueError('Source cleanup warning')
                    except Exception as exc: report['cleanup_errors'].append({'source':source_id,'error':str(exc)[:350]})
                save()
        negative=[]
        parent=check_response(client.post('agent/conversations',json={'title':'Gold unsupported temporary'}))['conv_id']
        try:
            for case in gold['negative_cases']:
                path=args.root/case['file']
                with path.open('rb') as stream:
                    response=client.post(f'agent/conversations/{parent}/attachments',files={'file':(path.name,stream)})
                passed=response.status_code==400 and 'supported' in response.text.lower()
                negative.append({'id':case['id'],'status':'passed' if passed else 'failed','http_status':response.status_code})
        finally:
            check_response(client.delete(f'agent/conversations/{parent}'))
        report['negative_http']=negative
    report['finished_at']=datetime.now(timezone(timedelta(hours=8))).isoformat()
    report['summary']={status:sum(q['status']==status for q in report['questions']) for status in {q['status'] for q in report['questions']}}
    report['screening_passed']=(report['http_regression']['passed']==report['http_regression']['total']
        and all(n['status']=='passed' for n in report['negative_http'])
        and len(report['questions'])==len(gold['questions'])
        and all(q['status']=='auto_pass_pending_semantic_review' and not q.get('cleanup_error') for q in report['questions'])
        and not report['cleanup_errors'])
    save();print(json.dumps(report['summary'],ensure_ascii=False),flush=True)
    sys.exit(0 if report['screening_passed'] else 1)


if __name__=='__main__': main()
