"""Freeze private acceptance labels from source OOXML, without application imports."""
import hashlib
import json
from pathlib import Path
import shutil
from decimal import Decimal
from zipfile import ZipFile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / 'data/business_acceptance/2026-10-15'
OUTPUT = ROOT / 'data/business_acceptance/gold-v1'
NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def source_cells(path):
    """Independent oracle: direct ZIP/XML, no application parser or Excel recalculation."""
    with ZipFile(path) as archive:
        strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            strings = [''.join(n.itertext()) for n in ET.fromstring(archive.read('xl/sharedStrings.xml')).findall('s:si', NS)]
        rels = {n.attrib['Id']: n.attrib['Target'] for n in ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))}
        result = {}
        for sheet in ET.fromstring(archive.read('xl/workbook.xml')).findall('s:sheets/s:sheet', NS):
            target = rels[sheet.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']]
            target = target.lstrip('/') if target.startswith('/') else 'xl/' + target
            cells = {}
            for c in ET.fromstring(archive.read(target)).findall('.//s:sheetData/s:row/s:c', NS):
                value = c.find('s:v', NS)
                if c.attrib.get('t') == 's' and value is not None:
                    raw = strings[int(value.text)]
                elif c.attrib.get('t') == 'inlineStr':
                    raw = ''.join(n.text or '' for n in c.findall('.//s:t', NS))
                elif value is not None:
                    raw = value.text
                else:
                    raw = None
                cells[c.attrib['r']] = {'value': raw, 'formula': c.findtext('s:f', namespaces=NS), 'type': c.attrib.get('t', 'n')}
            result[sheet.attrib['name']] = cells
        return result


def main():
    if (OUTPUT / 'gold.json').exists():
        raise SystemExit('Gold already frozen; create a new version rather than overwrite labels.')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    candidates = json.loads((CORPUS / 'acceptance-candidates.json').read_text(encoding='utf-8-sig'))
    specs = []
    def add(prefix, sheet, start, end, key, sums=(), numeric_key=False, region=None):
        specs.append(dict(prefix=prefix, sheet=sheet, first=start, last=end, key=key,
                          sums=list(sums), numeric_key=numeric_key, region=region))
    add('3f12', 'Sheet1', 5, 17, 'A', ('A',))
    add('83a5', '导线库', 2, 7, 'A', ('B', 'C', 'D'))
    add('d85a', '国网批次', 3, 100, 'A', numeric_key=True)
    for name, end in [('市区',677),('晋安',72),('仓山',52),('自贸区',70),('高新区',128),('长乐',159),('福清',449),('连江',90),('罗源',57),('闽侯',219),('闽清',96),('平潭',72),('永泰',142)]:
        add('dcb9', name, 2, end, 'E', ('H',))
    sources = {}
    for prefix in {s['prefix'] for s in specs}:
        candidate = next(c for c in candidates if Path(c['collected_file']).stem.startswith(prefix))
        sources[prefix] = CORPUS / candidate['collected_file']
    for prefix, filename in [('damage','损坏文件扫描报告_final.xlsx'),('duplicate','重复文件统计报告_原材料路径.xlsx')]:
        sources[prefix] = Path('D:/榕能电力审图项目资料') / filename
    add('damage','汇总统计',16,44,'A',('B',),region='损坏类型分布')
    add('damage','汇总统计',49,111,'A',('B',),region='文件类型分布 (全部)')
    add('duplicate','汇总统计',25,49,'A',('B',),region='文件类型分布')
    frozen_sources = {}
    xml = {}
    for prefix, path in sources.items():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        target = OUTPUT / 'files' / (digest + path.suffix.lower())
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(path, target)
        frozen_sources[prefix] = {'file': str(target.relative_to(OUTPUT)).replace('\\','/'), 'sha256': digest, 'filename':path.name}
        xml[prefix] = source_cells(target)
    cases = []
    questions = []
    for i, spec in enumerate(specs, 1):
        cells = xml[spec['prefix']][spec['sheet']]
        rows = []
        for row in range(spec['first'], spec['last']+1):
            value = cells.get(f'{spec["key"]}{row}', {}).get('value')
            if value is None or not str(value).strip():
                continue
            if spec['numeric_key']:
                try: Decimal(value)
                except Exception: continue
            rows.append(row)
        aggregates = []
        for col in spec['sums']:
            values = []
            for row in rows:
                cell = cells.get(f'{col}{row}', {})
                if cell.get('value') not in (None,''):
                    if cell.get('formula'):
                        raise ValueError('Formula totals require separately reviewed calculation semantics')
                    values.append(Decimal(cell['value']))
            aggregates.append({'source_column':col,'expected':str(sum(values,Decimal(0))), 'numeric_cells':len(values), 'tolerance':'0.000001'})
        case = {'id':f'G{i:03}', 'source':spec['prefix'],'sheet':spec['sheet'],'region':spec['region'],
                'source_range':f'{spec["key"]}{spec["first"]}:{spec["key"]}{spec["last"]}',
                'key_column':spec['key'],'expected_source_rows':rows,'expected_count':len(rows),'aggregates':aggregates}
        cases.append(case)
        questions.append({'id':case['id']+'-Q1','case_id':case['id'],
            'question':f'只统计“{spec["sheet"]}”工作表'+(f'的“{spec["region"]}”区域' if spec['region'] else '')+'，有效记录有多少条？不要与其他表合并。',
            'expected':{'count':len(rows)},'evidence':case['source_range'],
            'grading':'count_exact_and_correct_sheet_scope','execution_status':'not_run'})
        if aggregates:
            questions.append({'id':case['id']+'-Q2','case_id':case['id'],
                'question':f'只统计“{spec["sheet"]}”工作表'+(f'的“{spec["region"]}”区域' if spec['region'] else '')+f'，{spec["sums"][0]}列合计多少？按原始单位回答，不合并汇总表。',
                'expected':aggregates[0], 'evidence':{'rows':rows,'column':spec['sums'][0]},
                'grading':'numeric_tolerance_and_correct_sheet_scope','execution_status':'not_run'})
    negatives = []
    for candidate in candidates:
        src = CORPUS / candidate['collected_file']
        if src.suffix not in ('.xls','.et'): continue
        digest=hashlib.sha256(src.read_bytes()).hexdigest()
        target=OUTPUT/'files'/(digest+src.suffix)
        shutil.copyfile(src,target)
        negatives.append({'id':f'N{len(negatives)+1:03}','file':str(target.relative_to(OUTPUT)).replace('\\','/'),
                          'sha256':digest,'expected':'unsupported_format_rejected'})
    gold={'version':'gold-v1','oracle':'direct OOXML ZIP/XML + Decimal; no application parser imports',
          'review_status':'independently_computed_pending_business_signoff','human_reviewer':None,
          'sources':frozen_sources,'cases':cases,'negative_cases':negatives,'questions':questions,
          'scope_notes':['Region sheets and 汇总 are not additive; 汇总 is excluded from regional totals.',
                         'Source row count is not a claim of unique projects; no deduplication assumed.',
                         'Count/sum source regions are fixed annotations. Model execution is not yet evaluated.']}
    (OUTPUT/'gold.json').write_text(json.dumps(gold,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'cases':len(cases),'negative_cases':len(negatives),'questions':len(questions),'output':str(OUTPUT)},ensure_ascii=False))


if __name__ == '__main__':
    main()
