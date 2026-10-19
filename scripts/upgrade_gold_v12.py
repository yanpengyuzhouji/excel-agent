"""Freeze v1.2: previously blocked tower workbook now requires warning review."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil


root=Path(__file__).resolve().parents[1]/'data/business_acceptance'
source=root/'gold-v1.1'
target=root/'gold-v1.2'
if target.exists(): raise SystemExit('Gold v1.2 already exists; refusing overwrite')
gold=deepcopy(json.loads((source/'gold.json').read_text(encoding='utf-8')))
gold.update(version='gold-v1.2',parent_version='gold-v1.1',
            parent_sha256=hashlib.sha256((source/'gold.json').read_bytes()).hexdigest())
gold['sources']['83a5']['expected_attachment_status']='needs_review'
gold['scope_notes'].append('Tower workbook: verified parallel regions and formula-linked gaps; expected warning review instead of hard rejection. Numeric labels unchanged.')
target.mkdir(parents=True)
shutil.copytree(source/'files',target/'files')
(target/'gold.json').write_text(json.dumps(gold,ensure_ascii=False,indent=2),encoding='utf-8')
print('Frozen '+str(target))
