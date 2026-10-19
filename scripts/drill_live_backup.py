"""Approved brief cold backup; restore only to a new private Docker volume."""
from datetime import datetime, timezone, timedelta
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid
import httpx

ROOT = Path(__file__).resolve().parents[1]
LIVE = 'excel-workbook-service-excel-agent-1'
BASE = 'http://127.0.0.1:8091'


def docker(*args):
    value = subprocess.run(['docker', *args], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if value.returncode:
        raise RuntimeError(value.stderr[-1500:])
    return value.stdout.strip()


def ready(origin):
    start = time.monotonic()
    while time.monotonic()-start < 60:
        try:
            response = httpx.get(origin+'/api/v1/health', timeout=2, trust_env=False)
            if response.status_code == 200:
                return response.json()
        except httpx.HTTPError:
            pass
        time.sleep(.5)
    raise RuntimeError('Service did not become ready')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirm-cold-backup', action='store_true', help='Explicitly allow the core service to stop briefly')
    if not parser.parse_args().confirm_cold_backup:
        parser.error('Cold backup interrupts service; pass --confirm-cold-backup only during an approved maintenance window')
    tag = uuid.uuid4().hex[:12]
    clone, volume = 'excel-restore-'+tag, 'excel-restore-'+tag+'-data'
    out = ROOT/'data/business_acceptance'/('live-backup-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
    out.mkdir(parents=True, exist_ok=False)
    report = {'started_at': datetime.now(timezone(timedelta(hours=8))).isoformat(),
              'live_container': LIVE, 'restore_container': clone, 'restore_volume': volume, 'checks': []}
    def save():
        (out/'drill.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    def check(name, passed, **details):
        report['checks'].append(dict(name=name, passed=bool(passed), **details))
        save()
        print(name+' '+str(bool(passed)), flush=True)
        if not passed:
            raise RuntimeError('Recovery verification failed: '+name)
    def snapshot(vol):
        result = docker('run', '--rm', '--user', '0', '--network', 'none',
            '--mount', f'type=volume,source={vol},target=/data,readonly',
            '--mount', f'type=bind,source={ROOT / "scripts"},target=/verification,readonly',
            image, 'python', '/verification/volume_manifest.py', '/data')
        return json.loads(result)
    conv = None
    live_stopped = False
    print('REPORT '+str(out), flush=True)
    save()
    try:
        image = docker('inspect', '--format', '{{.Image}}', LIVE)
        mounts = json.loads(docker('inspect', '--format', '{{json .Mounts}}', LIVE))
        source = next(m for m in mounts if m['Destination'] == '/data' and m['Type'] == 'volume')['Name']
        assert source != volume and clone.startswith('excel-restore-') and volume.startswith('excel-restore-')
        report.update(image=image, source_volume=source)
        with httpx.Client(base_url=BASE, timeout=90, trust_env=False) as client:
            conv_response = client.post('/api/v1/agent/conversations', json={'title': 'backup drill temporary canary'})
            conv_response.raise_for_status()
            conv = conv_response.json()['conv_id']
            response = client.post(f'/api/v1/agent/conversations/{conv}/attachments',
                files={'file': ('recovery-canary.csv', b'id,amount\n00001,1\n00002,2\n00003,3\n')})
            response.raise_for_status()
            attachment = response.json()
            check('canary_attachment_ready', attachment['status'] == 'ready')
            imported = client.get('/api/v1/workbook-imports/'+attachment['import_id']).json()
            table = next(s for s in imported['sheets'] if s.get('enabled') and s.get('kind') == 'data')
            report_code = (
                'import json,sys;from app.config import build_app_config;from app.db import ensure_tables;'
                'from app.service.service import WorkbookImportService;from app.agent.tools import html_interpreter;'
                'from app.agent.session import AgentConversation;ensure_tables();'
                'a=html_interpreter(WorkbookImportService(build_app_config()),"",'
                '"<html><body><h1>Recovery verification</h1><p>Three records, amount 6.</p></body></html>",'
                '"Recovery verification",conversation_id=sys.argv[1]);'
                'assert not a.get("error"),a;'
                'c=AgentConversation.load(sys.argv[1],"anonymous");c.start_new_round();'
                'c.add_user("synthetic recovery verification");c.add_ai("Recovery report: "+a["url"]);'
                'print(json.dumps({"url":a["url"]}))')
            artifact = json.loads(docker('exec', LIVE, 'python', '-c', report_code, conv))
            original_html = client.get(artifact['url'])
            original_html.raise_for_status()
            baseline_conversations = client.get('/api/v1/agent/conversations').json()
            baseline_detail = client.get('/api/v1/agent/conversations/'+conv).json()
        report.update(canary_conv=conv, canary_workbook=attachment['workbook_id'])
        offline_start = time.monotonic()
        docker('stop', '--time', '30', LIVE)
        live_stopped = True
        try:
            original = snapshot(source)
            (out/'source-manifest.json').write_text(json.dumps(original, ensure_ascii=False, indent=2), encoding='utf-8')
            check('source_sqlite_integrity', bool(original['databases']) and all(d['integrity_ok'] for d in original['databases'].values()),
                  databases=len(original['databases']), files=original['file_count'], bytes=original['total_bytes'])
            docker('run', '--rm', '--user', '0', '--network', 'none',
                '--mount', f'type=volume,source={source},target=/source,readonly',
                '--mount', f'type=bind,source={out},target=/backup', image,
                'tar', 'cf', '/backup/data-volume.tar', '-C', '/source', '.')
        finally:
            docker('start', LIVE)
            live_stopped = False
            report['live_health_after_backup'] = ready(BASE)
            report['live_interruption_seconds'] = round(time.monotonic()-offline_start, 3)
            save()
        archive = out/'data-volume.tar'
        digest = hashlib.sha256()
        with archive.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024*1024), b''):
                digest.update(chunk)
        report['archive_sha256'] = digest.hexdigest()
        report['archive_bytes'] = archive.stat().st_size
        restore_start = time.monotonic()
        docker('volume', 'create', '--label', 'excel.acceptance=backup-drill', volume)
        docker('run', '--rm', '--user', '0', '--network', 'none',
            '--mount', f'type=volume,source={volume},target=/target',
            '--mount', f'type=bind,source={out},target=/backup,readonly', image,
            'tar', 'xf', '/backup/data-volume.tar', '-C', '/target')
        restored = snapshot(volume)
        (out/'restored-manifest.json').write_text(json.dumps(restored, ensure_ascii=False, indent=2), encoding='utf-8')
        check('all_file_hashes_match', original['files'] == restored['files'])
        check('all_sqlite_counts_and_integrity_match', original['databases'] == restored['databases'])
        docker('run', '-d', '--name', clone, '--memory', '2g', '--cpus', '2',
            '-p', '127.0.0.1:18093:8090', '--mount', f'type=volume,source={volume},target=/data',
            '-e', 'EXCEL_REQUIRE_AUTH=false', '-e', 'EXCEL_LLM_BASE_URL=http://127.0.0.1:1/v1',
            '-e', 'EXCEL_LLM_API_KEY=synthetic-disabled', image)
        ready('http://127.0.0.1:18093')
        with httpx.Client(base_url='http://127.0.0.1:18093', timeout=90, trust_env=False) as client:
            conversations = client.get('/api/v1/agent/conversations').json()
            check('all_conversation_ids_restored', {c['conv_id'] for c in conversations} == {c['conv_id'] for c in baseline_conversations})
            detail = client.get('/api/v1/agent/conversations/'+conv).json()
            check('canary_history_and_attachment_restored', detail['messages'] == baseline_detail['messages'] and detail['scope'] == baseline_detail['scope'])
            column = next(c['physical_name'] for c in table['columns'] if c['display_name'] == 'amount')
            query = client.post('/api/v1/workbooks/'+attachment['workbook_id']+'/query',
                json={'sql': 'SELECT COUNT(*),SUM("'+column+'") FROM "'+table['table_name']+'"'})
            query.raise_for_status()
            check('restored_query_correct', query.json()['rows'] == [[3, 6.0]])
            html = client.get(artifact['url'])
            check('restored_html_identical', html.status_code == 200 and html.content == original_html.content)
            pdf = client.get(artifact['url']+'/pdf')
            check('restored_pdf_download', pdf.status_code == 200 and pdf.content.startswith(b'%PDF-'))
        report['restore_verification_seconds'] = round(time.monotonic()-restore_start, 3)
    except Exception as exc:
        report['error'] = str(exc)[:1500]
        raise
    finally:
        if live_stopped:
            docker('start', LIVE)
        if conv:
            try:
                with httpx.Client(base_url=BASE, timeout=60, trust_env=False) as client:
                    response = client.delete('/api/v1/agent/conversations/'+conv)
                    response.raise_for_status()
                    report['live_canary_cleanup'] = bool(response.json().get('deleted'))
            except Exception as exc:
                report['cleanup_error'] = str(exc)[:500]
        # Only the UUID-named restoration resources created above may be removed.
        assert clone == 'excel-restore-'+tag and volume == clone+'-data'
        subprocess.run(['docker','rm','-f',clone], capture_output=True)
        subprocess.run(['docker','volume','rm',volume], capture_output=True)
        report['finished_at'] = datetime.now(timezone(timedelta(hours=8))).isoformat()
        report['passed'] = not report.get('error') and all(c['passed'] for c in report['checks']) and report.get('live_canary_cleanup', False)
        save()
        print(json.dumps({'output': str(out), 'passed': report['passed']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
