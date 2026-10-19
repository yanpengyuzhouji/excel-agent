"""Capture only safe per-call metadata for a regression before container replacement."""
import json
from pathlib import Path
import subprocess
import sys
import re
from collections import Counter

folder=Path(sys.argv[1]);report=json.loads((folder/'acceptance.json').read_text(encoding='utf-8'))
ids={q.get('conv_id') for q in report['questions']}
logs=subprocess.run(['docker','logs','--since',report['started_at'],'excel-workbook-service-excel-agent-1'],capture_output=True,text=True,encoding='utf-8',errors='replace',check=True)
calls=[]
envelopes=Counter()
for line in (logs.stdout+'\n'+logs.stderr).splitlines():
    if 'tool_envelope_normalized ' in line:
        match=re.search(r'tool_envelope_normalized action=([a-z_]+) promoted=([a-z_,]+)',line)
        if match:envelopes[match[1]+':'+match[2]]+=1
    if 'llm_request ' not in line:continue
    try:value=json.loads(line.split('llm_request ',1)[1])
    except ValueError:continue
    if value.get('conv_id') in ids:calls.append(value)
result={'calls':calls,'total_attempts':len(calls),'outcomes':dict(Counter(c['outcome'] for c in calls)),
        'recovery_attempts':sum(c.get('attempt',1)>1 for c in calls),
        'service_envelope_normalizations_since_start':dict(envelopes),
        'envelope_scope':'Service-wide since start; normalization logs have no conversation ID, unlike filtered LLM calls.',
        'note':'Logical question results include bounded internal recovery; raw model attempt outcomes remain visible.'}
(folder/'model-diagnostics.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in result.items() if k!='calls'},ensure_ascii=False))
