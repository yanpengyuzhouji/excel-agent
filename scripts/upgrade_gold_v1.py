"""Create v1.1 from frozen v1, adding source-cell mappings and admission expectations."""
import json
from pathlib import Path
import shutil

from build_gold_v1 import OUTPUT, source_cells


def main():
    target=OUTPUT.with_name('gold-v1.1')
    if target.exists():
        raise SystemExit('v1.1 exists; refusing to overwrite frozen labels')
    gold=json.loads((OUTPUT/'gold.json').read_text(encoding='utf-8'))
    target.mkdir(parents=True)
    shutil.copytree(OUTPUT/'files',target/'files')
    gold['version']='gold-v1.1'
    gold['parent_version']='gold-v1'
    cells={key:source_cells(target/source['file']) for key,source in gold['sources'].items()}
    for case in gold['cases']:
        columns={case['key_column']} | {a['source_column'] for a in case['aggregates']}
        if case['id']=='G001': columns.add('B')
        if case['id']=='G003': columns.update(('C','D','I','J'))
        case['expected_cells']={f'{column}{row}':cells[case['source']][case['sheet']].get(f'{column}{row}',{'value':None,'type':'n','formula':None})
                                for row in case['expected_source_rows'] for column in sorted(columns)}
    expectations={'3f12':'ready','83a5':'blocked','d85a':'needs_review','dcb9':'needs_review','damage':'ready','duplicate':'ready'}
    for key,source in gold['sources'].items():
        source['expected_attachment_status']=expectations[key]
    (target/'gold.json').write_text(json.dumps(gold,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'version':gold['version'],'cell_checks':sum(len(c['expected_cells']) for c in gold['cases']),
                      'sources':len(gold['sources']),'path':str(target)},ensure_ascii=False))


if __name__=='__main__': main()
