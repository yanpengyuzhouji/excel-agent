"""One independent recheck of execution-error questions; preserve baseline results."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import sys

import httpx
from run_gold_agent import ask
from run_gold_http import check_response


def main():
    run=Path(sys.argv[1]).resolve()
    root=run.parent.parent
    baseline=json.loads((run/'acceptance.json').read_text(encoding='utf-8'))
    gold=json.loads((root/'gold.json').read_text(encoding='utf-8'))
    if hashlib.sha256((root/'gold.json').read_bytes()).hexdigest()!=baseline['gold_sha256']:
        raise ValueError('Gold labels differ from baseline')
    selected={q['id'] for q in baseline['questions'] if q['status']=='execution_error'}
    output=run/'retry-once.json'
    if output.exists(): raise SystemExit('Independent recheck already exists; refusing another retry.')
    result={'baseline':'acceptance.json','model':baseline['model'],'results':[],
            'started_at':datetime.now(timezone.utc).isoformat(),'cleanup_errors':[]}
    def save(): output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    with httpx.Client(base_url=baseline['base_url'].rstrip('/')+'/',timeout=300,trust_env=False) as client:
        for source_id,source in gold['sources'].items():
            questions=[q for q in gold['questions'] if q['id'] in selected and
                       next(c for c in gold['cases'] if c['id']==q['case_id'])['source']==source_id]
            if not questions: continue
            parent=None
            try:
                if hashlib.sha256((root/source['file']).read_bytes()).hexdigest()!=source['sha256']:
                    raise ValueError('Gold source changed')
                parent=check_response(client.post('agent/conversations',json={'title':'Gold one-time recheck source'}))['conv_id']
                with (root/source['file']).open('rb') as stream:
                    item=check_response(client.post(f'agent/conversations/{parent}/attachments',files={'file':(source['filename'],stream)}))
                if item['status']=='needs_review':
                    item=check_response(client.post(f'agent/conversations/{parent}/attachments/{item["import_id"]}/confirm',
                        json={'revision':item['revision'],'accept_severe_warnings':True}))
                if item['status']!='ready': raise ValueError('Recheck source not ready')
                imported=check_response(client.get(f'workbook-imports/{item["import_id"]}'))
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures=[]
                    for question in questions:
                        case=next(c for c in gold['cases'] if c['id']==question['case_id'])
                        table=next(s for s in imported['sheets'] if s['name']==case['sheet'] and
                                   (case['region'] is None or s.get('region_title')==case['region']))
                        futures.append(pool.submit(ask,baseline['base_url'],baseline['model'],question,case,table['table_name'],item['workbook_id']))
                    for future in as_completed(futures):
                        row=future.result();result['results'].append(row);save();print(row['id']+' '+row['status'],flush=True)
            finally:
                if parent:
                    deleted=check_response(client.delete(f'agent/conversations/{parent}'))
                    if deleted.get('cleanup_warnings'):result['cleanup_errors'].append(source_id)
                save()
    result['finished_at']=datetime.now(timezone.utc).isoformat()
    result['passed']=sum(r['status']=='auto_pass_pending_semantic_review' for r in result['results'])
    result['total']=len(selected)
    save();print(json.dumps({'passed':result['passed'],'total':result['total']}),flush=True)
    sys.exit(0 if result['passed']==result['total'] else 1)


if __name__=='__main__': main()
