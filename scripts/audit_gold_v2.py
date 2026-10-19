"""Post-run evidence audit; never rewrites first-attempt model results."""
from collections import Counter
import json
from pathlib import Path
import re
import sys

folder=Path(sys.argv[1])
report=json.loads((folder/'acceptance.json').read_text(encoding='utf-8'))
suite=json.loads((folder.parents[1]/'suite.json').read_text(encoding='utf-8'))
cases={c['id']:c for c in suite['cases']}
items=[];history={}
for result in sorted(report['questions'],key=lambda q:list(cases).index(q['id'])):
    case=cases[result['id']]
    events=json.loads((folder/case['id']/'events.json').read_text(encoding='utf-8'))
    queries=[e for e in events if e.get('type')=='source.query.result']
    group=case.get('conversation')
    evidence=history.get(group,[])+queries if group else queries
    targets=[]
    for target in case.get('tables',[]):
        source=report['prepared_sources'][target['source']]
        sheets=[s for s in source['imported']['sheets'] if s['name']==target['sheet'] and (not target.get('region') or s.get('region_title')==target['region'])]
        name=sheets[0]['table_name'] if len(sheets)==1 else None
        matched=False
        for q in evidence:
            names=re.findall(r'\b(?:FROM|JOIN)\s+(?:"([^"]+)"|`([^`]+)`|\[([^]]+)\]|([^\s;,()]+))',q.get('sql',''),re.I)
            referenced={next(v for v in m if v) for m in names}
            if name in referenced and q.get('source',{}).get('workbook_id')==source['attachment']['workbook_id']:matched=True
        targets.append({'source':target['source'],'table':name,'exact_table_and_workbook_match':matched})
    if group:history[group]=evidence
    repeated=Counter(re.sub(r'\s+',' ',q.get('sql','')).strip().lower() for q in queries)
    refusal=None
    if case.get('kind')=='refusal':refusal=any(v in result.get('answer','') for v in case.get('refusal_terms',[])) and not (result.get('done') or {}).get('report_url')
    items.append({'id':case['id'],'initial_status':result['status'],'query_count':len(queries),
        'tool_steps':sum(e.get('type')=='step.start' for e in events),
        'context_compressions':sum(e.get('type')=='context.compacted' for e in events),
        'repeated_successful_sql':{s:n for s,n in repeated.items() if n>1},
        'truncated_queries':sum(bool(q.get('truncated')) for q in queries),
        'evidence_targets':targets,'refusal_screening':refusal})
write={'items':items,'total':len(items),'scope_failures':[i['id'] for i in items if any(not t['exact_table_and_workbook_match'] for t in i['evidence_targets'])],
       'note':'Separate audit, not a replacement for first-attempt or semantic results.'}
(folder/'execution-audit.json').write_text(json.dumps(write,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'total':len(items),'scope_failures':write['scope_failures']},ensure_ascii=False))
