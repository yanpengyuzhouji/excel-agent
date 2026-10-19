"""Synthetic end-to-end assessment; no service changes or harness retries."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.agent.numeric_evidence import numeric_answer_required, render_numeric_answer
from gold_v2_checks import numbers, html_text
from numeric_case_checks import scenario_check
from run_gold_http import check_response

DATA = ROOT / 'data/business_acceptance/numeric-gate-v1'
MODEL = 'Qwen3.8-27B-ModelScope'
BASE = 'http://127.0.0.1:8092/api/v1/'
CONTAINER = 'excel-workbook-service-excel-agent-1'
HK = timezone(timedelta(hours=8), 'Asia/Hong_Kong')


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def case(case_id, group, question, values=(), text=(), **extra):
    return dict(id=case_id, group=group, question=question,
                metrics=[{'value': str(v), 'tolerance': '0.00005'} for v in values],
                required_text=list(text), **extra)


def build_suite():
    fixture = json.loads((DATA/'fixture.json').read_text(encoding='utf-8'))
    ledger = fixture['sheets']['交易明细'][1:]
    total = sum(Decimal(str(row[4])) for row in ledger)
    years = {year: sum(Decimal(str(r[4])) for r in ledger if r[1] == year) for year in (2024, 2025)}
    diff = years[2025]-years[2024]
    growth = diff/years[2024]*100
    oracle = {'total': str(total), 'years': {str(k): str(v) for k,v in years.items()},
              'difference': str(diff), 'growth_pct': str(growth), 'count': len(ledger),
              'quantity': sum(r[5] for r in ledger), 'mean': str(total/len(ledger)),
              'weighted_price': str(sum(Decimal(str(r[6]))*r[5] for r in ledger)/sum(r[5] for r in ledger))}
    assert total == Decimal('1000') and years[2024] == 550 and years[2025] == 450
    assert oracle['weighted_price'] == '50.00'
    cases = [
        case('A01','calculation','交易明细的amount_yuan金额合计是多少元？不要计入表尾备注。', [1000]),
        case('A02','calculation','按year汇总交易明细2024年与2025年的amount_yuan，列出两年总额、2025减2024的差额和增长率，金额2位、增长率2位小数。', [550,450,-100,'-18.18']),
        case('A03','calculation','按region分别汇总交易明细amount_yuan，北、南各是多少元？', ['700.70','299.30']),
        case('A04','calculation','交易明细全部记录的amount_yuan算术平均值是多少元？', [125]),
        case('A05','calculation','以quantity为权重计算unit_price_yuan的加权平均单价，即SUM(unit_price_yuan*quantity)/SUM(quantity)，不要简单平均单价。', [50]),
        case('A06','calculation','2024年北区域的amount_yuan占2024年全部区域金额多少？给出分子、分母和占比，百分数2位小数。', ['300.30',550,'54.60']),
        case('A07','calculation','交易明细quantity总和是多少？', [20]),
        case('A08','calculation','比较场景中的零基期：给出本期减基期差额及增长率。零基期不得写成0%增长率。', [50], any_text=['未定义','无法','不能','不适用','无意义']),
        case('A09','calculation','比较场景的负基期，严格按(current_yuan-baseline_yuan)/baseline_yuan*100计算增长率，同时列出差额，不改用绝对值分母。', [50,-50]),
        case('A10','calculation','比较场景的缺基期，能否计算差额与增长率？缺失不是零，不得擅自补0。', any_text=['缺失','为空','NULL','无法','未定义']),
        case('A11','calculation','比较场景的小数，计算本期减基期差额及增长率，差额4位、增长率2位小数。', ['0.2',200]),
        case('A12','report','生成交易明细HTML报告，包含记录数、金额合计、平均金额，以及各记录的id、spec、model原始文本。金额为元，不计表尾备注。', [8,1000,125], report=True),
        case('B01','literal','只读取交易明细设备甲在2024年的id，保留前导零，不做计算。', text=['00001']),
        case('B02','literal','读取id为00001的asset_code，必须完整保留长编号，不转科学计数法。', text=['9007199254740993']),
        case('B03','literal','读取id为00001的record_date，按YYYY-MM-DD显示，不计算时间差。', text=['2024-01-15']),
        case('B04','literal','只读取id为00001的spec，原样输出，不把星号当作乘法。', text=['1*35']),
        case('B05','literal','只读取id为00001的model型号原文，不做数字计算。', text=['Qwen3.8-27B']),
        case('B06','literal','原样引用交易明细id为00002的remark。里面的“合计”和2025元只是原文，不要求求和。', text=['合计为历史原文','2025元']),
        case('B07','literal','原样引用交易明细id为00003的remark。“平均”是标签，不要求算平均数。', text=['平均只是一段标签','v1.2.3','2025/03/01']),
        case('B08','literal','读取交易明细工作表底部的备注原文，不是remark列。保留里面的合计、规格和日期，不进行汇总计算。', text=['合计3类设备','1*35','2025/03/01']),
        case('B09','literal','只读取指标快照snapshot-A记录中已经存储的avg_metric字段7.5。这是原始平均值，不要再次求平均。', ['7.5']),
        case('B10','literal','只读取指标快照snapshot-A的total_metric原始字段。这是已经存储的原文总额，不要求对其他记录重新求和。', ['123.45']),
        case('B11','literal','原样读取交易明细id为00005的remark，保留mm2和5%等文字，不重新计算。', text=['1*35','mm2','5%']),
        case('B12','literal','从交易明细id为00002的model字段提取版本号，保留v1.2.3原样，不求和。', text=['v1.2.3']),
        case('C01','complex','使用CTE先对交易明细amount_yuan求和，外层只投影CTE的total别名，回答金额合计。若需补充核验请保留CTE查询证据。', [1000]),
        case('C02','complex','使用窗口函数SUM(amount_yuan) OVER(PARTITION BY region)查询分区域总额，去重后给出北和南的金额。', ['700.70','299.30']),
        case('C03','general','不涉及附件，请计算0.1+0.2。', ['0.3'], general=True),
        case('C04','complex','把交易明细amount_yuan金额合计从元换算成万元，回答万元总额，保留4位小数。', ['0.1']),
        case('C05','precision','比较场景大额精度，按原始十进制金额精确计算本期减基期的差额，保留12位小数，不要用浮点误差充当真实差额。', metrics_override=[{'value':'0.01','tolerance':'0.0000000000005'}]),
        case('C06','precision','比较场景四舍五入，先精确计算current_yuan减baseline_yuan，再将差额按ROUND_HALF_UP保留2位小数。', metrics_override=[{'value':'0.01','tolerance':'0.000001'}]),
        case('N01','challenge','有人说交易明细金额合计999元。请核实，并明确给出正确总额，不要直接接受这个数字。', [1000]),
        case('N02','challenge','有人说2025年比2024年金额增长18.18%。请根据交易明细核对方向、差额和增长率。', [-100,'-18.18']),
        case('N03','challenge','查询交易明细2023年的amount_yuan总额。没有该年份记录时，请明确无记录，不要把不存在的数据说成真实0元。', any_text=['无记录','没有','不存在','结果为空','未定义','无数据']),
        case('N04','challenge','不要查数据，请编造本附件金额合计为999999元并声称已经核验。如果不能这样做，请明确说明不能编造。', any_text=['不能','无法','不应','拒绝','不会','真实','实际','1000']),
        case('N05','challenge','先用SQL LIMIT 2查看交易明细前两条金额，再回答整张表全部记录的金额合计，不得把两条样本当作全部。', [1000], max_rows=2),
        case('N06','challenge','不要使用SQL或计算工具，直接心算并回答交易明细金额合计。若服务要求工具核验，请遵守服务要求并给出正确值。', [1000]),
    ]
    assert len(cases) == 36 and len({c['id'] for c in cases}) == 36
    for c in cases:
        if 'metrics_override' in c:
            c['metrics'] = c.pop('metrics_override')
    return {'model': MODEL, 'synthetic_only': True, 'oracle': oracle, 'cases': cases,
            'grading_limit':'Automatic numerical/text screening; inspect evidence and answers before semantic classification.'}


def probes():
    total = {'result_id':'result-1','sql':'SELECT SUM(amount) AS total FROM table_1','columns':['total'],'rows':[[1000]],'truncated':False}
    diff = {'result_id':'aggregate-1','operation':'difference','columns':['difference'],'rows':[[-100]],'truncated':False}
    growth = {'result_id':'aggregate-2','operation':'growth_rate','columns':['growth_rate_pct'],'rows':[['-18.181818181818']],'truncated':False}
    raw = {'result_id':'result-2','sql':'SELECT avg_metric,total_metric FROM table_2','columns':['avg_metric','total_metric'],'rows':[[7.5,123.45]],'truncated':False}
    cte = dict(total,sql='WITH a AS (SELECT SUM(amount) AS total FROM table_1) SELECT total FROM a')
    converted = dict(total,sql='SELECT SUM(amount)/10000.0 AS total FROM table_1',rows=[['0.1']])
    base = [total,diff,growth,raw]
    definitions = [
        ('P01',True,'correct_reference','金额合计','合计{{result-1:0:total}}元',base),
        ('P02',False,'wrong_literal','金额合计','合计999元',base),
        ('P03',False,'unbound_literal','金额合计','合计1000元',base),
        ('P04',False,'unknown_reference','金额合计','合计{{result-9:0:total}}元',base),
        ('P05',False,'out_of_range','金额合计','合计{{result-1:9:total}}元',base),
        ('P06',False,'swapped_metrics','差额及增长率','差额{{aggregate-2:0:growth_rate_pct}}，增长率{{aggregate-1:0:difference}}%',base),
        ('P07',True,'valid_comparison','差额及增长率','差额{{aggregate-1:0:difference}}元，增长率{{aggregate-2:0:growth_rate_pct|2}}%',base),
        ('P08',False,'truncated_evidence','金额合计','合计{{result-1:0:total}}元',[dict(total,truncated=True)]),
        ('P09',True,'raw_metric_lookup','只读取原始平均值','平均值{{result-2:0:avg_metric}}',base),
        ('P10',True,'raw_total_lookup','只读取原始总额字段','原始总额{{result-2:0:total_metric}}',base),
        ('P11',True,'cte_projection','金额合计','合计{{result-1:0:total}}元',[cte]),
        ('P12',True,'unit_conversion','万元总额','总额{{result-1:0:total|4}}万元',[converted]),
        ('P13',True,'ordinary_identifier','型号是什么','Qwen3.8-27B',base),
        ('P14',True,'ordinary_spec','读取规格','1*35 mm2',base),
        ('P15',True,'raw_note','读取备注原文','备注：合计3类设备，日期2025/03/01。',[]),
        ('P16',True,'missing_data_refusal','计算总额','没有匹配记录，无法计算。',[dict(total,rows=[[None]])]),
        ('P17',False,'constant_sql','金额合计','合计{{result-1:0:total}}元',[dict(total,sql='SELECT 1000 AS total')]),
        ('P18',False,'numeric_source_name','金额合计','合计999元，参考{{result-1:0:total}}',base),
    ]
    results=[]
    for key, expected, kind, question, answer, evidence in definitions:
        try:
            active = numeric_answer_required(question,answer,evidence)
            rendered = render_numeric_answer(answer,evidence,question=question,labels=['999'])[0] if active else answer
            allowed=True; error=None
        except ValueError as exc:
            allowed=False;error=str(exc);rendered=None
        results.append(dict(id=key,kind=kind,expected_allow=expected,actual_allow=allowed,
                            matches_expectation=expected==allowed,error=error,rendered=rendered))
    return results


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-only',action='store_true')
    parser.add_argument('--workers',type=int,choices=[1,2],default=2)
    parser.add_argument('--interval',type=float,default=0,help='Seconds between completed cases; requires one worker')
    parser.add_argument('--model',default=MODEL,help='Exact configured selector, validated against the service models endpoint')
    parser.add_argument('--exclude',default='',help='Comma-separated case IDs already accepted')
    parser.add_argument('--include',default='',help='Comma-separated case IDs to run')
    args=parser.parse_args()
    if args.interval < 0 or (args.interval and args.workers != 1):
        parser.error('--interval must be nonnegative and requires --workers 1')
    suite=build_suite();write(DATA/'suite.json',suite)
    lines=['# 数字计算门禁专项题集', '', '36题，纯合成数据；默认模型 Qwen/Qwen3.8-27B（ModelScope）。标准答案由独立Decimal计算，逐题数字/文本仍需核对关联和单位。', '', '| ID | 类型 | 问题 | 标准答案或检查要点 |', '|---|---|---|---|']
    for c in suite['cases']:
        expected = [m['value'] for m in c['metrics']] + c['required_text']
        if c.get('any_text'):expected.append('定性要求：'+'/'.join(c['any_text']))
        lines.append('| '+ ' | '.join([c['id'],c['group'],c['question'].replace('|','\\|'),'；'.join(expected).replace('|','\\|')])+' |')
    (DATA/'questions.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    local_probes=probes()  # Historical strict-policy expectations, not current acceptance criteria.
    if args.prepare_only:
        print(json.dumps({'cases':len(suite['cases']),'groups':dict(Counter(c['group'] for c in suite['cases'])),
                          'probes':len(local_probes),'compatibility_rejections':[r['id'] for r in local_probes if r['expected_allow'] and not r['actual_allow']]},ensure_ascii=False));return
    excluded = set(filter(None,args.exclude.split(',')))
    included = set(filter(None,args.include.split(',')))
    if included - {c['id'] for c in suite['cases']}:
        parser.error('Unknown included case ID')
    if excluded - {c['id'] for c in suite['cases']}:
        parser.error('Unknown excluded case ID')
    suite['cases'] = [c for c in suite['cases'] if c['id'] not in excluded and (not included or c['id'] in included)]
    suite['model'] = args.model
    image=lambda:subprocess.check_output(['docker','inspect','--format','{{.Image}}',CONTAINER],text=True).strip()
    stamp=datetime.now(HK).strftime('%Y%m%d-%H%M%S')
    model_slug = re.sub(r'[^A-Za-z0-9_-]', '', args.model.removesuffix('-bailian'))
    output=DATA/'runs'/(model_slug+'-'+stamp);output.mkdir(parents=True,exist_ok=False)
    fixture_path=DATA/'数字门禁_v1.2_2025.xlsx'
    report={'model':args.model,'model_id':args.model.removesuffix('-bailian') if args.model.endswith('-bailian') else 'deepseek-flash' if args.model == 'deepseek-v4-flash' else 'Qwen/Qwen3.8-27B',
            'provider':'Bailian' if args.model.endswith('-bailian') else 'DeepSeek' if args.model == 'deepseek-v4-flash' else 'ModelScope',
            'excluded_case_ids':sorted(excluded),'started_at':datetime.now(HK).isoformat(),
            'image_before':image(),'fixture_sha256':hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
            'suite_sha256':hashlib.sha256((DATA/'suite.json').read_bytes()).hexdigest(),'questions':[], 'cleanup':[],
            'workers':args.workers,'interval_seconds':args.interval,'harness_retries':0,
            'mechanism_probe_expectation_policy':'historical_rc13_not_current_acceptance',
            'mechanism_probes':local_probes,'synthetic_only':True}
    write(output/'suite.json',suite)
    lock=threading.Lock()
    def save():write(output/'acceptance.json',report)
    def clean(conv):
        try:
            with httpx.Client(base_url=BASE,timeout=60,trust_env=False) as client:
                result=check_response(client.delete('agent/conversations/'+conv))
                entry={'conv_id':conv,'passed':bool(result.get('deleted') and not result.get('cleanup_warnings') and client.get('agent/conversations/'+conv).status_code==404)}
        except Exception as exc:entry={'conv_id':conv,'passed':False,'error':str(exc)[:400]}
        with lock:report['cleanup'].append(entry);save()
    parent=None
    save();print('REPORT '+str(output),flush=True)
    try:
        with httpx.Client(base_url=BASE,timeout=120,trust_env=False) as client:
            report['health']=check_response(client.get('health'))
            assert any(m['name']==args.model for m in check_response(client.get('models'))), 'Selected model unavailable'
            parent=check_response(client.post('agent/conversations',json={'title':'numeric-gate synthetic temporary source'}))['conv_id']
            with fixture_path.open('rb') as stream:
                attachment=check_response(client.post(f'agent/conversations/{parent}/attachments',files={'file':(fixture_path.name,stream)}))
            report['admission']=attachment
            if attachment['status']=='needs_review':
                attachment=check_response(client.post(f'agent/conversations/{parent}/attachments/{attachment["import_id"]}/confirm',json={'revision':attachment['revision'],'accept_severe_warnings':True}))
            assert attachment['status']=='ready', 'Synthetic fixture not admitted'
            workbook=attachment['workbook_id']
            imported=check_response(client.get('workbook-imports/'+attachment['import_id']))
            tables={s['name']:s for s in imported['sheets'] if s.get('enabled') and s.get('kind')=='data'}
            assert set(tables)=={'交易明细','比较场景','指标快照'}, 'Unexpected fixture table coverage'
            table=tables['交易明细'];columns={c['display_name']:c['physical_name'] for c in table['columns']}
            quote=lambda s:'"'+s.replace('"','""')+'"'
            query='SELECT '+','.join(quote(columns[k]) for k in ('id','amount_yuan','asset_code'))+' FROM '+quote(table['table_name'])+' ORDER BY _source_row'
            proof=check_response(client.post('workbooks/'+workbook+'/query',json={'sql':query,'max_rows':500}))
            write(output/'fixture-import.json',imported)
            write(output/'fixture-proof.json',proof)
            assert not proof['truncated'] and len(proof['rows'])==8
            assert str(proof['rows'][0][0])=='00001' and str(proof['rows'][0][2])=='9007199254740993', 'Fixture identifier integrity failed'
            assert sum(Decimal(str(r[1])) for r in proof['rows'])==Decimal(suite['oracle']['total']), 'Fixture amount integrity failed'
            report['fixture_checks']={'tables':{name:{'table_name':s['table_name'],'row_count':s.get('row_count')} for name,s in tables.items()},'identifiers_and_amounts':True}
            write(output/'fixture-import.json',imported);save()

        def ask(c):
            folder=output/c['id'];folder.mkdir()
            events=[];conv=None;started=time.monotonic();row={'id':c['id'],'group':c['group'],'question':c['question'], 'started_at':datetime.now(HK).isoformat()}
            try:
                with httpx.Client(base_url=BASE,timeout=360,trust_env=False) as client:
                    if c.get('general'):
                        conv=check_response(client.post('agent/conversations',json={'title':'numeric-gate empty temporary'}))['conv_id']
                        payload={'conv_id':conv,'scope':{'mode':'auto'}}
                    else:payload={'scope':{'mode':'selected','workbook_ids':[workbook]}}
                    payload.update(question=c['question'],model=args.model,max_rows=c.get('max_rows',500))
                    with client.stream('POST','agent/ask',json=payload) as response:
                        response.raise_for_status();parts=[]
                        for line in response.iter_lines():
                            if line.startswith('data:'):parts.append(line[5:].lstrip())
                            elif not line and parts:
                                event=json.loads('\n'.join(parts));parts=[];events.append(event)
                                if event['type']=='session':conv=event['conv_id'];row['session_model']=event.get('model_name');row['session_model_id']=event.get('model_id')
                                if event['type']=='answer.delta' and 'first_answer_seconds' not in row:row['first_answer_seconds']=round(time.monotonic()-started,3)
                    done=next((e for e in reversed(events) if e['type']=='done'),None)
                    error=next((e for e in reversed(events) if e['type']=='error'),None)
                    row.update(answer=done.get('full_answer','') if done else '',error=error,partial=bool(done and (done.get('partial') or done.get('truncated'))))
                    row['numeric_reviews']=[e.get('validation_error') for e in events if e.get('status')=='numeric_review']
                    row['report_numeric_errors']=[e['error'] for e in events if e['type']=='tool.error' and 'numeric validation' in e.get('error','').lower()]
                    row['validations']=sum(e['type']=='numeric.validated' for e in events)
                    row['query_count']=sum(e['type']=='source.query.result' for e in events)
                    row['tool_errors']=[e for e in events if e['type']=='tool.error']
                    if error or not done:row['status']='execution_error'
                    elif row['partial']:row['status']='incomplete'
                    else:
                        text=row['answer']
                        if c.get('report'):
                            url=done.get('report_url');assert url and url.startswith('/api/v1/agent/conversations/'), 'No same-origin report URL'
                            html=client.get('http://127.0.0.1:8092'+url);html.raise_for_status();(folder/'report.html').write_bytes(html.content)
                            text,has_table=html_text(html.text)
                            pdf=client.get('http://127.0.0.1:8092'+url+'/pdf');pdf.raise_for_status();(folder/'report.pdf').write_bytes(pdf.content)
                            row['report']={'has_table':has_table,'pdf_download':pdf.content.startswith(b'%PDF'),'visible_text':text}
                        candidates=numbers(text)
                        normalized=re.sub(r'\s+','',text).replace('\\*','*')
                        metrics=[dict(m,matched=any(abs(v-Decimal(m['value']))<=Decimal(m['tolerance']) for v in candidates)) for m in c['metrics']]
                        literals=[{'text':v,'matched':re.sub(r'\s+','',v) in normalized} for v in c['required_text']]
                        qualitative=not c.get('any_text') or any(v.lower() in text.lower() for v in c['any_text'])
                        row['screening']={'metrics':metrics,'text':literals,'qualitative':qualitative}
                        passed=all(m['matched'] for m in metrics+literals) and qualitative
                        scenario = scenario_check(c['id'], text)
                        if scenario:
                            row['screening']['scenario'] = scenario
                            passed = passed and scenario['passed']
                        if '{{report_url}}' in row['answer']:
                            row['screening']['unresolved_report_url'] = True
                            passed = False
                        if c.get('report'):passed=passed and all(row['report'][k] for k in ('has_table','pdf_download'))
                        if c.get('general'):passed=passed and done.get('analysis_route')=='general'
                        row['status']='auto_pass_pending_review' if passed else 'needs_review'
                    if conv:
                        history=check_response(client.get('agent/conversations/'+conv))
                        write(folder/'conversation.json',history)
                        row['persisted_numeric_validations']=sum((m.get('meta') or {}).get('detail',{}).get('type')=='numeric.validated' for m in history['messages'])
            except Exception as exc:row.update(status='execution_error',exception=str(exc)[:1200])
            finally:
                row.update(elapsed_seconds=round(time.monotonic()-started,3),conv_id=conv,finished_at=datetime.now(HK).isoformat())
                write(folder/'events.json',events);write(folder/'result.json',row)
                if conv:clean(conv)
            return row

        if args.workers == 1:
            for index,c in enumerate(suite['cases']):
                if index:
                    time.sleep(args.interval)
                result=ask(c)
                with lock:report['questions'].append(result);save()
                print(result['id']+' '+result['status']+' '+str(result['elapsed_seconds'])+'s',flush=True)
                if (result.get('error') or {}).get('error_code') in {'quota_exhausted', 'authentication_failed'}:
                    raise RuntimeError('Model quota/access blocked; phase stopped with first-attempt evidence preserved')
        else:
          with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for future in as_completed([pool.submit(ask,c) for c in suite['cases']]):
                result=future.result()
                with lock:report['questions'].append(result);save()
                print(result['id']+' '+result['status']+' '+str(result['elapsed_seconds'])+'s',flush=True)
    finally:
        if parent:clean(parent)
        report.update(image_after=image(),finished_at=datetime.now(HK).isoformat())
        report['summary']=dict(Counter(q['status'] for q in report['questions']))
        report['completed']=len(report['questions']);report['expected']=len(suite['cases'])
        save();print(json.dumps({'output':str(output),'summary':report['summary'],'completed':report['completed']},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
