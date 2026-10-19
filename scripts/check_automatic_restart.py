"""Verify Docker restart policy after process crash and real OOM, on fresh volumes."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time
import uuid
import httpx

ROOT = Path(__file__).resolve().parents[1]


def docker(*args):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.returncode:
        raise RuntimeError(result.stderr[-1000:])
    return result.stdout.strip()


def main():
    image = docker('inspect', '--format', '{{.Image}}', 'excel-workbook-service-excel-agent-1')
    out = ROOT/'data/business_acceptance'/('auto-restart-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
    out.mkdir(parents=True)
    report = {'image': image, 'checks': [], 'scope': 'Fresh synthetic containers only; unless-stopped policy, no manual start after injection'}
    print('REPORT '+str(out), flush=True)
    for index, mode in enumerate(['crash', 'oom']):
        tag = uuid.uuid4().hex[:12]
        name, volume = 'excel-autorestart-'+tag, 'excel-autorestart-'+tag+'-data'
        since = datetime.now(timezone.utc).isoformat()
        code = ('import os,time;from pathlib import Path;p=Path("/data/fault-injected");'
                'first=not p.exists();p.touch();time.sleep(11 if first else 0);'
                + ('os._exit(7) if first else None;' if mode == 'crash' else 'a=bytearray(1024*1024*1024) if first else None;')
                + 'os.execvp("python",["python","run.py"])')
        started = time.monotonic()
        row = {'mode': mode, 'container': name, 'passed': False}
        try:
            docker('run', '-d', '--name', name, '--restart', 'unless-stopped', '--init',
                   '--memory', '256m', '--memory-swap', '256m', '--cpus', '2',
                   '--security-opt', 'no-new-privileges:true', '-p', f'127.0.0.1:{18094+index}:8090',
                   '--mount', f'type=volume,source={volume},target=/data',
                   '-e', 'EXCEL_REQUIRE_AUTH=false', image, 'python', '-c', code)
            port = json.loads(docker('inspect', '--format', '{{json .NetworkSettings.Ports}}', name))['8090/tcp'][0]['HostPort']
            until = time.monotonic()+80
            while time.monotonic() < until:
                try:
                    response = httpx.get(f'http://127.0.0.1:{port}/api/v1/health', timeout=2, trust_env=False)
                    if response.status_code == 200:
                        row['health'] = response.json()
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.5)
            restarts = int(docker('inspect', '--format', '{{.RestartCount}}', name))
            events = [json.loads(line) for line in docker('events', '--since', since,
                '--until', datetime.now(timezone.utc).isoformat(), '--filter', 'container='+name,
                '--format', '{{json .}}').splitlines() if line.strip()]
            (out/(mode+'-events.json')).write_text(json.dumps(events, indent=2), encoding='utf-8')
            (out/(mode+'-container.log')).write_text(docker('logs', name), encoding='utf-8')
            observed = any(e.get('Action') == ('oom' if mode == 'oom' else 'die') for e in events)
            row.update(restarts=restarts, fault_event_observed=observed,
                seconds_to_healthy=round(time.monotonic()-started, 3),
                passed=restarts >= 1 and observed and row.get('health', {}).get('status') == 'ok')
        except Exception as exc:
            row['error'] = str(exc)[:1200]
        finally:
            assert name == 'excel-autorestart-'+tag and volume == name+'-data'
            subprocess.run(['docker','rm','-f',name], capture_output=True)
            subprocess.run(['docker','volume','rm',volume], capture_output=True)
            report['checks'].append(row)
            (out/'restart.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(mode+' '+str(row['passed']), flush=True)
    report['passed'] = all(row['passed'] for row in report['checks'])
    (out/'restart.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
