"""Isolated Docker capacity/crash/restore acceptance; no business data or paid model."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time
import traceback
import uuid
import httpx

ROOT=Path(__file__).resolve().parents[1]

def docker(*args,check=True):
    r=subprocess.run(['docker',*args],capture_output=True,text=True,encoding='utf-8',errors='replace')
    if check and r.returncode:raise RuntimeError(r.stderr[-1000:])
    return (r.stdout+r.stderr if args and args[0]=='logs' else r.stdout).strip()

class Provider(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length','0')))
        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
        try:
            for text,finish in [('synthetic-',None),('ok','stop')]:
                data={'id':'synthetic','object':'chat.completion.chunk','created':0,'model':'ops-stub','choices':[{'index':0,'delta':{'content':text},'finish_reason':finish}]}
                self.wfile.write(('data: '+json.dumps(data)+'\n\n').encode());self.wfile.flush()
                if not finish:time.sleep(1.5)
            self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush()
        except (BrokenPipeError,ConnectionResetError):pass

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=18091)
    parser.add_argument('--image',help='Exact prebuilt image tag/ID; defaults to current service image')
    parser.add_argument('--agent-limit',type=int,default=2,help='Expected limit in the frozen image, not a runtime override')
    parser.add_argument('--soak-seconds',type=int,default=900)
    args=parser.parse_args()
    if not 1 <= args.agent_limit <= 32 or args.soak_seconds < 60:
        parser.error('agent-limit must be 1..32 and soak-seconds must be at least 60')
    base=f'http://127.0.0.1:{args.port}/api/v1/'
    tag=uuid.uuid4().hex[:10];name='excel-ops-'+tag;volume=name+'-data';restored=name+'-restored'
    out=ROOT/'data/business_acceptance'/('release-ops-'+datetime.now().strftime('%Y%m%d-%H%M%S'));out.mkdir(parents=True)
    image=(docker('image','inspect','--format','{{.Id}}',args.image) if args.image else
           docker('inspect','--format','{{.Image}}','excel-workbook-service-excel-agent-1'))
    report={'started_at':datetime.now(timezone(timedelta(hours=8))).isoformat(),'image':image,'checks':[],'resources':[],
            'scope':'Isolated single-instance 2CPU/2GiB; synthetic provider tests admission/recovery only, not model quality.'}
    def save():(out/'ops.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    def record(label,ok,**details):
        report['checks'].append({'name':label,'passed':bool(ok),**details});save();print(label+' '+str(bool(ok)),flush=True)
    def start(vol):
        docker('run','-d','--name',name,'--memory','2g','--cpus','2','--mount',f'type=volume,source={vol},target=/data',
               '-p',f'127.0.0.1:{args.port}:8090','-e',f'EXCEL_LLM_BASE_URL=http://host.docker.internal:{provider.server_port}/v1',
               '-e','EXCEL_LLM_MODEL=ops-stub','-e','EXCEL_LLM_API_KEY=synthetic-key',image)
    def ready():
        until=time.monotonic()+60
        while time.monotonic()<until:
            try:
                if httpx.get(base+'health',trust_env=False,timeout=2).status_code==200:return
            except httpx.HTTPError:pass
            time.sleep(.5)
        raise RuntimeError('Isolated service did not become ready')
    def ask(index):
        conv=None
        with httpx.Client(base_url=base,timeout=30,trust_env=False) as c:
            with c.stream('POST','agent/ask',json={'question':f'synthetic load {index}','scope':{'mode':'selected','workbook_ids':[]}}) as r:
                if r.status_code!=200:return {'status':r.status_code,'retry_after':r.headers.get('retry-after')}
                final=None
                for line in r.iter_lines():
                    if not line.startswith('data:'):continue
                    e=json.loads(line[5:])
                    if e.get('type')=='session':conv=e['conv_id']
                    if e.get('type') in ('done','error'):final=e
            return {'status':200,'done':bool(final and final.get('type')=='done'),'conv_id':conv,
                    'error_code':(final or {}).get('error_code')}
    stop=threading.Event()
    def monitor():
        while not stop.wait(2):
            value=docker('stats','--no-stream','--format','{{json .}}',name,check=False)
            if value:
                try:
                    s=json.loads(value);report['resources'].append({k:s.get(k) for k in ('MemUsage','MemPerc','CPUPerc')})
                except ValueError:pass
    provider=ThreadingHTTPServer(('0.0.0.0',0),Provider)
    threading.Thread(target=provider.serve_forever,daemon=True).start()
    print('REPORT '+str(out),flush=True);save()
    try:
        start(volume);ready();threading.Thread(target=monitor,daemon=True).start()
        # Control-plane checks span imports and container restarts. Do not reuse
        # Windows/Docker Desktop idle TCP connections across those boundaries.
        with httpx.Client(base_url=base,timeout=600,trust_env=False,
                          limits=httpx.Limits(max_keepalive_connections=0)) as c:
            def cleanup_batch(results):
                # Lifecycle fences may reject deletion while a parallel run is
                # active; clean only after the whole batch has completed.
                for result in results:
                    if result.get('conv_id'):
                        for delay in (.05,.1,.2,.4,.8,1.6):
                            # Cleanup is not a keep-alive soak: the long-lived
                            # coordinator connection may be closed at idle timeout.
                            with httpx.Client(base_url=base,timeout=30,trust_env=False) as cleaner:
                                deleted=cleaner.delete('agent/conversations/'+result['conv_id'])
                            if deleted.status_code!=429:break
                            report['cleanup_busy_retries']=report.get('cleanup_busy_retries',0)+1
                            time.sleep(delay)
                        deleted.raise_for_status()
                        assert c.get('agent/conversations/'+result['conv_id']).status_code==404
            conv=c.post('agent/conversations',json={'title':'synthetic recovery fixture'}).json()['conv_id']
            started=time.monotonic()
            # Synthetic parser fixture, not a user-facing authored spreadsheet.
            body=('id,amount,weight,c3,c4,c5\n'+''.join(f'{i},{i%1000},1,2,3,4\n' for i in range(1,100000))).encode()
            def upload():return c.post(f'agent/conversations/{conv}/attachments',files={'file':('capacity.csv',body,'text/csv')})
            with ThreadPoolExecutor(max_workers=2) as pool:
                first=pool.submit(upload);time.sleep(.3)
                second=pool.submit(upload);responses=[first.result(),second.result()]
            accepted=[r for r in responses if r.status_code==200]
            record('import_capacity_and_admission',len(accepted)==1 and any(r.status_code==429 for r in responses),
                   statuses=[r.status_code for r in responses],rows_including_header=100000,cells=600000,elapsed_seconds=round(time.monotonic()-started,2))
            if not accepted:raise RuntimeError('No capacity import succeeded')
            attachment=accepted[0].json()
            if attachment['status']=='needs_review':
                response=c.post(f'agent/conversations/{conv}/attachments/{attachment["import_id"]}/confirm',json={'revision':attachment['revision'],'accept_severe_warnings':True});response.raise_for_status();attachment=response.json()
            record('capacity_attachment_ready',attachment['status']=='ready')
            imported=c.get('workbook-imports/'+attachment['import_id']).json()
            table=next(s['table_name'] for s in imported['sheets'] if s.get('kind')=='data' and s.get('enabled'))
            query={'sql':f'SELECT COUNT(*) FROM "{table}"','max_rows':1}
            def count():
                r=c.post(f'workbooks/{attachment["workbook_id"]}/query',json=query);r.raise_for_status();return r.json()['rows'][0][0]
            record('capacity_row_integrity',count()==99999)
            barrier=threading.Barrier(12)
            def expensive_sql(_):
                barrier.wait()
                with httpx.Client(base_url=base,timeout=30,trust_env=False) as client:
                    response=client.post(f'workbooks/{attachment["workbook_id"]}/query',json={
                        'sql':f'SELECT SUM(a._source_row*b._source_row) FROM "{table}" a CROSS JOIN "{table}" b','max_rows':1})
                    return response.status_code
            with ThreadPoolExecutor(max_workers=12) as pool:sql_statuses=list(pool.map(expensive_sql,range(12)))
            record('sql_budget_and_concurrency',429 in sql_statuses and all(s in (400,429) for s in sql_statuses),statuses=sql_statuses)
            for filename,payload,label in [('over-rows.csv',b'id,value\n'+b'1,2\n'*100000,'row_limit_rejection'),
                                            ('over-cells.csv',('a,b,c,d,e,f,g\n'+'1,2,3,4,5,6,7\n'*86000).encode(),'cell_limit_rejection')]:
                response=c.post(f'agent/conversations/{conv}/attachments',files={'file':(filename,payload)})
                record(label,response.status_code in (400,413,422),status=response.status_code)
            with ThreadPoolExecutor(max_workers=args.agent_limit+1) as pool:
                results=list(pool.map(ask,range(args.agent_limit+1)))
            record('agent_concurrency_backpressure',sum(r['status']==429 for r in results)>=1 and sum(r.get('done',False) for r in results)==args.agent_limit,
                   expected_limit=args.agent_limit,results=results)
            cleanup_batch(results)
            # Bounded fifteen-minute soak; deterministic provider avoids API spend.
            started=time.monotonic();soak=[]
            with ThreadPoolExecutor(max_workers=2) as pool:
                while time.monotonic()-started<args.soak_seconds:
                    batch=list(pool.map(ask,range(2)));soak.extend(batch);cleanup_batch(batch)
            record('streaming_soak_'+str(args.soak_seconds)+'s',all(r.get('done') for r in soak),requests=len(soak),seconds=round(time.monotonic()-started,2),
                   failures=[r for r in soak if not r.get('done')])
            docker('restart',name);ready();record('restart_persistence',count()==99999 and c.get('agent/conversations/'+conv).status_code==200)
            docker('kill','--signal=KILL',name)
            state=json.loads(docker('inspect','--format','{{json .State}}',name))
            record('forced_crash_observed',state.get('ExitCode')==137,oom_killed=state.get('OOMKilled'))
            docker('start',name);ready();record('crash_recovery',count()==99999)
            # Fault injection is limited to this UUID-named synthetic container.
            assert name.startswith('excel-ops-')
            docker('update','--memory','32m','--memory-swap','32m',name)
            until=time.monotonic()+20
            while time.monotonic()<until:
                state=json.loads(docker('inspect','--format','{{json .State}}',name))
                if not state.get('Running'):break
                time.sleep(.2)
            record('actual_oom_observed',state.get('OOMKilled') and state.get('ExitCode')==137,state=state)
            docker('update','--memory','2g','--memory-swap','4g',name)
            if state.get('Running'):docker('restart',name)
            else:docker('start',name)
            ready();record('oom_restart_recovery',count()==99999)
            docker('stop',name)
            archive=out/'synthetic-volume-backup.tar'
            docker('run','--rm','--user','0','--mount',f'type=volume,source={volume},target=/source,readonly',
                   '--mount',f'type=bind,source={out},target=/backup',image,'tar','cf','/backup/synthetic-volume-backup.tar','-C','/source','.')
            report['backup_sha256']=hashlib.sha256(archive.read_bytes()).hexdigest();save()
            docker('rm',name)
            docker('run','--rm','--user','0','--mount',f'type=volume,source={restored},target=/target',
                   '--mount',f'type=bind,source={out},target=/backup,readonly',image,'tar','xf','/backup/synthetic-volume-backup.tar','-C','/target')
            start(restored);ready();record('backup_restore_independent_volume',count()==99999 and c.get('agent/conversations/'+conv).status_code==200)
    except Exception as e:
        record('unhandled_failure',False,error=str(e)[-1500:],trace=traceback.format_exc())
    finally:
        stop.set();provider.shutdown()
        (out/'container.log').write_text(docker('logs',name,check=False),encoding='utf-8')
        docker('rm','-f',name,check=False)
        for vol in (volume,restored):docker('volume','rm',vol,check=False)
        report['finished_at']=datetime.now(timezone(timedelta(hours=8))).isoformat()
        report['passed']=bool(report['checks']) and all(v['passed'] for v in report['checks']);save()
        print(json.dumps({'output':str(out),'passed':report['passed']}),flush=True)
    return 0 if report['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
