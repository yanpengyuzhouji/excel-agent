"""Private golden regression: no harness retries; service recovery is audited separately."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from collections import Counter
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import httpx
from run_gold_http import run as run_http, check_response
from gold_v2_checks import text_checks, html_text, packet_checks

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data/business_acceptance/gold-v2'
BASE='http://127.0.0.1:8092/api/v1/'
ORIGIN='http://127.0.0.1:8092'
MODEL='deepseek-v4-flash'
BUNDLED=Path('C:/Users/18779/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe')

def now():return datetime.now(timezone(timedelta(hours=8))).isoformat()
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
def image_id():
    return subprocess.check_output(['docker','inspect','--format','{{.Image}}','excel-workbook-service-excel-agent-1'],text=True).strip()
def helper(python,script,*args):
    env=dict(os.environ,PYTHONIOENCODING='utf-8')
    result=subprocess.run([str(python),str(ROOT/'scripts'/script),*map(str,args)],capture_output=True,text=True,encoding='utf-8',env=env,timeout=120)
    if result.returncode:raise RuntimeError((result.stderr or result.stdout)[-1800:])
    return json.loads(result.stdout.strip().splitlines()[-1])

def main():
    global MODEL
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recheck',type=Path,help='Baseline run directory; retry execution failures and recorded semantic defects only')
    parser.add_argument('--case', action='append', help='Run explicitly selected independent cases; does not count as a full golden run')
    parser.add_argument('--clarification', help='Append explicit scope to ONE independent selected case; supplemental probe, never a full run')
    parser.add_argument('--workers', type=int, choices=[1,2], default=2)
    parser.add_argument('--model', default=MODEL, help='Exact configured selector, validated against the service models endpoint')
    parser.add_argument('--interval', type=float, default=0, help='Seconds between turns; requires --workers 1')
    args=parser.parse_args()
    MODEL = args.model
    if args.interval < 0 or (args.interval and args.workers != 1):
        parser.error('Nonnegative --interval requires --workers 1')
    suite=json.loads((DATA/'suite.json').read_text(encoding='utf-8'))
    gold=json.loads((DATA/'gold.json').read_text(encoding='utf-8'))
    baseline=None
    if args.case:
        selected_cases = [c for c in suite['cases'] if c['id'] in set(args.case)]
        if len(selected_cases) != len(set(args.case)) or args.recheck:
            raise ValueError('Select known cases; cannot combine --case and --recheck')
        groups = {c.get('conversation') for c in selected_cases if c.get('conversation')}
        if any(c['id'] not in set(args.case) for c in suite['cases'] if c.get('conversation') in groups):
            raise ValueError('Select every turn of each dependent conversation')
        suite['cases'] = selected_cases
    if args.recheck:
        baseline=json.loads((args.recheck/'acceptance.json').read_text(encoding='utf-8'))
        if baseline['suite_sha256']!=hashlib.sha256((DATA/'suite.json').read_bytes()).hexdigest():
            raise ValueError('Suite differs from baseline')
        selected={q['id'] for q in baseline['questions'] if q['status']=='execution_error'}
        delivery=args.recheck/'delivery.json'
        if delivery.exists():
            selected.update(v['id'] for v in json.loads(delivery.read_text(encoding='utf-8'))['semantic_observations'] if v['verdict']=='semantic_defect')
        suite['cases']=[c for c in suite['cases'] if c['id'] in selected]
        if not suite['cases'] or any(c.get('conversation') for c in suite['cases']):
            raise ValueError('Empty selection or dependent multi-turn retry requires explicit full conversation selection')
    needed={s for c in suite['cases'] for s in c['sources']}
    original_question = None
    if args.clarification:
        if not args.case or len(suite['cases']) != 1 or suite['cases'][0].get('conversation') or args.recheck:
            parser.error('--clarification requires one independent --case and no --recheck')
        original_question = suite['cases'][0]['question']
        suite['cases'][0]['question'] += '\n补充说明：'+args.clarification
    model_slug = re.sub(r'[^A-Za-z0-9_-]', '', MODEL.removesuffix('-bailian'))
    output=DATA/'runs'/(model_slug+'-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
    output.mkdir(parents=True,exist_ok=False)
    report={'started_at':now(),'model':MODEL,'image_before':image_id(),'suite_sha256':hashlib.sha256((DATA/'suite.json').read_bytes()).hexdigest(),
            'workers':args.workers,'interval_seconds':args.interval,
            'questions':[],'prepared_sources':{},'cleanup':[],'grading_limit':'Automated screening only; business semantic and visual signoff remain separate.'}
    if baseline:
        report.update(baseline=str(args.recheck.resolve()),baseline_image=baseline['image_after'],
                      same_image_as_baseline=baseline['image_after']==report['image_before'],recheck_ids=sorted(selected))
    if args.case:
        report.update(selected_case_ids=sorted(set(args.case)), full_golden_run=False)
    if args.clarification:
        report.update(original_question=original_question, clarification=args.clarification,
                      supplemental_probe=True, full_golden_run=False)
    lock=threading.Lock()
    def save():write(output/'acceptance.json',report)
    def prepared(key,value):report['prepared_sources'][key]=value;save()
    def clean(conv):
        try:
            with httpx.Client(base_url=BASE,timeout=60,trust_env=False) as c:
                value=check_response(c.delete('agent/conversations/'+conv))
                ok=value.get('deleted') and not value.get('cleanup_warnings') and c.get('agent/conversations/'+conv).status_code==404
                result={'conv_id':conv,'passed':bool(ok),'warnings':value.get('cleanup_warnings')}
        except Exception as e:result={'conv_id':conv,'passed':False,'error':str(e)[:600]}
        with lock:report['cleanup'].append(result);save()
    save();print('REPORT '+str(output),flush=True)

    def ask(case,conv=None,history=None):
        folder=output/case['id'];folder.mkdir(exist_ok=True);write(folder/'case.json',case)
        events=[];started=time.monotonic();current=conv;result={'id':case['id'],'dimension':case['dimension'],'question':case['question']}
        try:
            sources=report['prepared_sources']
            if any(k not in sources for k in case['sources']):raise ValueError('Fixture preparation failed')
            ids=[sources[k]['attachment']['workbook_id'] for k in case['sources']]
            payload={'question':case['question'],'model':MODEL,'scope':{'mode':'selected','workbook_ids':ids},'max_rows':500}
            if conv:payload['conv_id']=conv
            with httpx.Client(base_url=BASE,timeout=360,trust_env=False) as c:
                with c.stream('POST','agent/ask',json=payload) as response:
                    response.raise_for_status();parts=[]
                    for line in response.iter_lines():
                        if line.startswith('data:'):parts.append(line[5:].lstrip())
                        elif not line and parts:
                            e=json.loads('\n'.join(parts));parts=[];events.append(e)
                            if e.get('type')=='session':
                                current=e['conv_id']
                                result['session_model_id']=e.get('model_id')
                done=next((e for e in reversed(events) if e.get('type')=='done'),None)
                error=next((e for e in reversed(events) if e.get('type')=='error'),None)
                result.update(answer=done.get('full_answer','') if done else '',done=done,error=error,conv_id=current)
                if error or not done:result['status']='execution_error'
                elif done.get('partial') or done.get('truncated'):result['status']='incomplete'
                else:
                    evidence=(history or [])+events;queries=[e for e in evidence if e.get('type')=='source.query.result']
                    checks=[]
                    for target in case.get('tables',[]):
                        sheets=[s for s in sources[target['source']]['imported']['sheets'] if s['name']==target['sheet'] and (not target.get('region') or s.get('region_title')==target['region'])]
                        if len(sheets)!=1:raise ValueError('Ambiguous oracle table '+str(target))
                        table=sheets[0]['table_name']
                        checks.append({'table':table,'queried':any(table in q.get('sql','') for q in queries)})
                    result['evidence_tables']=checks
                    result['text_checks']=text_checks(case,result['answer'])
                    passed=result['text_checks']['passed'] and all(t['queried'] for t in checks)
                    if done.get('analysis'):
                        result['packet_checks'] = packet_checks(case, done['analysis'])
                        passed = passed and result['packet_checks']['passed']
                    if case.get('kind')=='report':
                        url=done.get('report_url') or next((e.get('url') or e.get('report_url') for e in reversed(events) if e.get('type')=='html'),None)
                        if not url:raise ValueError('No report URL emitted')
                        parsed=urlsplit(url)
                        if parsed.netloc and parsed.netloc!='127.0.0.1:8092':raise ValueError('Unexpected report origin')
                        absolute=ORIGIN+url if url.startswith('/') else url
                        if '/api/v1/agent/conversations/' not in absolute:raise ValueError('Unexpected report path')
                        html=c.get(absolute);html.raise_for_status();(folder/'report.html').write_bytes(html.content)
                        text,table=html_text(html.text)
                        result['html']={'url':url,'table':table,'checks':text_checks(case,text),'headers':{k:html.headers.get(k) for k in ('content-type','content-security-policy','x-content-type-options')}}
                        pdf=c.get(absolute+'/pdf');pdf.raise_for_status();(folder/'report.pdf').write_bytes(pdf.content)
                        result['pdf']=helper(BUNDLED,'inspect_gold_pdf.py',folder/'report.pdf',folder/'case.json')
                        try:result['ui']=helper(sys.executable,'check_gold_report_ui.py',current,folder)
                        except Exception as e:result['ui']={'error':str(e)}
                        passed=passed and table and result['html']['checks']['passed'] and result['pdf']['checks']['passed'] and result['ui'].get('pdf_download',False) and not result['ui'].get('page_errors')
                    result['status']='auto_pass_pending_semantic_review' if passed else 'needs_review'
        except Exception as e:result.update(status='execution_error',exception=str(e)[:2000],conv_id=current)
        finally:
            write(folder/'events.json',events)
            result['elapsed_seconds']=round(time.monotonic()-started,3)
            write(folder/'result.json',result)
        return result,current,events

    def job(cases):
        conv=None;history=[]
        try:
            for case in cases:
                if args.interval and report['questions']:
                    time.sleep(args.interval)
                result,conv,events=ask(case,conv,history);history+=events
                with lock:report['questions'].append(result);save()
                print(case['id']+' '+result['status']+' '+str(result['elapsed_seconds'])+'s',flush=True)
                if (result.get('error') or {}).get('error_code') in {'quota_exhausted', 'authentication_failed'}:
                    raise RuntimeError('Model quota/access blocked; phase stopped with first-attempt evidence preserved')
        finally:
            if conv:clean(conv)

    try:
        report['http']=run_http(DATA,BASE,selected_sources=needed,confirm_warnings=True,keep_sources=True,on_prepared=prepared);save()
        with httpx.Client(base_url=BASE,timeout=90,trust_env=False) as c:
            if not any(m['name']==MODEL for m in check_response(c.get('models'))):raise ValueError('Selected model unavailable')
            parent=check_response(c.post('agent/conversations',json={'title':'gold-v2 negative temporary'}))['conv_id']
            try:
                report['negative_http']=[]
                for case in ([] if baseline else gold['negative_cases']):
                    path=DATA/case['file']
                    with path.open('rb') as f:r=c.post(f'agent/conversations/{parent}/attachments',files={'file':(path.name,f)})
                    report['negative_http'].append({'id':case['id'],'http_status':r.status_code,'passed':r.status_code==400 and 'supported' in r.text.lower()})
            finally:clean(parent)
        ordinary=[c for c in suite['cases'] if not c.get('conversation') and c.get('kind')!='report']
        if args.workers == 1:
            for c in ordinary:job([c])
        else:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                for future in as_completed([pool.submit(job,[c]) for c in ordinary]):future.result()
        for c in suite['cases']:
            if c.get('kind')=='report' and not c.get('conversation'):job([c])
        groups={c['conversation'] for c in suite['cases'] if c.get('conversation')}
        for group in sorted(groups):job(sorted([c for c in suite['cases'] if c.get('conversation')==group],key=lambda c:c['turn']))
    finally:
        for source in report['prepared_sources'].values():clean(source['conv_id'])
        report['finished_at']=now();report['image_after']=image_id();report['summary']=dict(Counter(q['status'] for q in report['questions']))
        report['expected_turns']=len(suite['cases']);report['completed_turns']=len(report['questions'])
        report['screening_passed']=(report['completed_turns']==report['expected_turns'] and
            all(q['status']=='auto_pass_pending_semantic_review' for q in report['questions']) and
            all(v['passed'] for v in report['cleanup']) and report['image_before']==report['image_after'] and
            report.get('http',{}).get('passed')==len(needed) and
            all(v['passed'] for v in report.get('negative_http',[])))
        save()
        print(json.dumps({'output':str(output),'summary':report['summary'],'completed':report['completed_turns']},ensure_ascii=False),flush=True)
    return 0 if report['screening_passed'] else 1

if __name__=='__main__':sys.exit(main())
