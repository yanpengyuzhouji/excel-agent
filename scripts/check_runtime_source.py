"""Compare deployed application/lock hashes without reading environment secrets."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
code = '''import json,hashlib
from pathlib import Path
r=Path('/app')
files=list((r/'app').rglob('*.py'))+[r/'config.yaml',r/'requirements.lock']
print(json.dumps({str(p.relative_to(r)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}))
'''
result = subprocess.run(['docker','exec','excel-workbook-service-excel-agent-1','python','-c',code],
    capture_output=True, text=True, encoding='utf-8', check=True)
hashes = json.loads(result.stdout)
different = [name for name, digest in hashes.items()
             if not (ROOT/name).is_file() or hashlib.sha256((ROOT/name).read_bytes()).hexdigest() != digest]
report = {'files_checked': len(hashes), 'different': different, 'hashes': hashes}
Path(sys.argv[1]).write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps({'files_checked': len(hashes), 'different': different}))
raise SystemExit(bool(different))
