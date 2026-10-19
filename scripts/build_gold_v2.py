"""Freeze independent real-source oracles for complex, conversational and report tests."""
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import csv
import hashlib
import json
from pathlib import Path
import shutil

from build_gold_v1 import source_cells

ROOT=Path(__file__).resolve().parents[1]/'data/business_acceptance'
OLD=ROOT/'gold-v1.2'
OUT=ROOT/'gold-v2'


def number(v):
    try:
        n=Decimal(str(v))
        return n if n.is_finite() else None
    except (InvalidOperation,ValueError): return None


def rounded(n,places=4):
    return str(n.quantize(Decimal(10)**-places,rounding=ROUND_HALF_UP))


def main():
    if OUT.exists(): raise SystemExit('Frozen v2 exists; refusing overwrite')
    g=json.loads((OLD/'gold.json').read_text(encoding='utf-8'))
    OUT.mkdir(parents=True)
    shutil.copytree(OLD/'files',OUT/'files')
    g.update(version='gold-v2',parent_version='gold-v1.2',parent_sha256=hashlib.sha256((OLD/'gold.json').read_bytes()).hexdigest())
    xml={k:source_cells(OUT/s['file']) for k,s in g['sources'].items()}
    suite=[]
    def metric(label,value,unit='',places=None):
        return {'label':label,'value':rounded(value,places) if places is not None else str(value),
                'unit':unit,'tolerance':str(Decimal(10)**-places/2) if places is not None else '0.000001'}
    def add(id,dimension,question,sources,tables,metrics=(),texts=(),**extra):
        suite.append({'id':id,'dimension':dimension,'question':question+' 请简明列出所要求的结果和来源。',
                      'sources':sources,'tables':tables,'metrics':list(metrics),'required_text':list(texts),**extra})
    for q in g['questions']:
        c=next(c for c in g['cases'] if c['id']==q['case_id'])
        val=q['expected'].get('count',q['expected'].get('expected'))
        add(q['id'],'basic',q['question'],[c['source']],[{'source':c['source'],'sheet':c['sheet'],'region':c['region']}],
            [metric('count' if 'count' in q['expected'] else 'sum',Decimal(str(val)))],
            evidence=q['evidence'],legacy_case_id=c['id'])
    region_cases=[c for c in g['cases'] if c['source']=='dcb9']
    rows={}
    for c in region_cases:
        rows[c['sheet']]=[{col:xml['dcb9'][c['sheet']].get(f'{col}{r}',{}).get('value')
                          for col in ['D','E','H','K','N','Z']} | {'row':r} for r in c['expected_source_rows']]
    def total(records,col='H'): return sum((number(r.get(col)) or Decimal(0) for r in records),Decimal(0))
    totals={name:total(rs) for name,rs in rows.items()}
    top=sorted(totals,key=lambda name:(-totals[name],name))[:3]
    all_total=sum(totals.values(),Decimal(0))
    a,b=totals['市区'],totals['福清']
    def tables(*names):return [{'source':'dcb9','sheet':n} for n in names]
    ab=[metric('市区',a,'万元',4),metric('福清',b,'万元',4),metric('差额',a-b,'万元',4),metric('比值',a/b,'倍',4)]
    add('C01','comparison','只比较市区和福清的H列投资，分别列出总额、市区减福清差额、市区/福清比值；金额和比值均保留4位小数，不合并汇总表。',['dcb9'],tables('市区','福清'),ab,['市区','福清'])
    add('C02','ranking','排除汇总和空白表，对13个区域的H列投资分别求和，列出投资前三名及各自占13区域总额的百分比。金额4位、占比2位小数。',['dcb9'],tables(*rows),
        [m for n in top for m in [metric(n,totals[n],'万元',4),metric(n+'占比',totals[n]/all_total*100,'%',2)]],top,required_order=top)
    add('C03','scope_and_double_counting','只合计13个区域表，排除汇总表：列出E列非空记录总数及H列投资总额，金额保留4位小数。不要按工程名去重。',['dcb9'],tables(*rows),
        [metric('记录数',sum(len(r) for r in rows.values())),metric('总投资',all_total,'万元',4)])
    selected=[r for r in rows['市区'] if number(r['K'])==2022 and (number(r['H']) or 0)>=100]
    add('C04','multiple_filters','市区表中K列=2022且H列>=100万元的记录有多少条，投资合计多少？不按名称去重，金额4位小数。',['dcb9'],tables('市区'),[metric('记录数',len(selected)),metric('投资',total(selected),'万元',4)])
    groups={name:[r for r in rows['市区'] if (r['Z'] or '').strip()==name] for name in ['新建','改造']}
    add('C05','grouping_and_share','市区表按Z列新建、改造分别列出记录数、投资合计及占市区总投资比例；另列其他或空白类型记录数。金额4位、比例2位小数。',['dcb9'],tables('市区'),
        [m for n,rs in groups.items() for m in [metric(n+'条数',len(rs)),metric(n+'投资',total(rs),'万元',4),metric(n+'占比',total(rs)/a*100,'%',2)]]+
        [metric('其他或空白',len(rows['市区'])-sum(map(len,groups.values())))],['新建','改造'])
    weighted=[r for r in rows['市区'] if number(r['N']) is not None and number(r['N'])>0 and number(r['H']) is not None]
    numerator=sum((number(r['H'])*number(r['N']) for r in weighted),Decimal(0));denominator=total(weighted,'N')
    add('C06','weighted_average','市区表仅取N列配变台数为正数且H列有数值的记录。以N为权重计算H的加权平均：SUM(H*N)/SUM(N)。列出分子、分母及加权平均，4位小数；这是给定公式的统计量。',['dcb9'],tables('市区'),
        [metric('分子',numerator,places=4),metric('分母',denominator),metric('加权平均',numerator/denominator,'万元',4)])
    distinct=len({str(r['E']).strip() for r in rows['晋安']})
    add('C07','deduplication','晋安表分别统计E列非空记录数、E列去首尾空白后的不同名称数，以及前两项之差。不要把名称重复等同于错误数据。',['dcb9'],tables('晋安'),
        [metric('记录数',len(rows['晋安'])),metric('不同名称',distinct),metric('差',len(rows['晋安'])-distinct)])
    invalid=[r for r in rows['福清'] if r['K'] not in (None,'') and (number(r['K']) is None or number(r['K'])!=int(number(r['K'])) or not 2000<=number(r['K'])<=2100)]
    missing=[r for r in rows['福清'] if r['K'] in (None,'')]
    add('C08','data_quality','福清表K列年份：空值单独统计；非空但不是2000至2100之间整数的记为异常。列出空值数、异常数和异常值示例，不修改源数据。',['dcb9'],tables('福清'),
        [metric('空值',len(missing)),metric('异常',len(invalid))],sorted({str(r['K']) for r in invalid}),evidence_rows=[r['row'] for r in invalid])
    multi=[r for r in rows['市区'] if r['D']=='A' and r['Z']=='新建' and number(r['K'])==2022]
    add('C09','compound_conditions','市区表同时满足D列=A、Z列=新建、K列=2022的记录数和H列合计是多少？金额4位小数。',['dcb9'],tables('市区'),[metric('记录数',len(multi)),metric('投资',total(multi),'万元',4)])
    add('C10','missing_data_refusal','请计算这份工作簿各月实际回款的同比增长率；若缺少实际回款金额、月份或上年同期数据，请明确说明缺失，不能用投资估算替代。',['dcb9'],[],kind='refusal',refusal_terms=['缺少','没有','不包含','无法'])
    wire=xml['83a5']['导线库']
    wires=[{'name':wire[f'A{r}']['value'],'diameter':number(wire[f'B{r}']['value']),'mass':number(wire[f'C{r}']['value']),'force':number(wire[f'D{r}']['value'])} for r in range(2,8)]
    wire_table=[{'source':'83a5','sheet':'导线库'}]
    filtered=[w for w in wires if w['diameter']>=22]
    add('C11','filter_average','只查导线库：外径>=22mm的型号有多少个？列出型号，并计算这些型号计算拉断力的算术平均值，保留2位小数。',['83a5'],wire_table,
        [metric('数量',len(filtered)),metric('平均拉断力',sum(w['force'] for w in filtered)/len(filtered),'N',2)],[w['name'] for w in filtered])
    leaders=sorted(wires,key=lambda w:-w['force'])[:3]
    add('C12','top_n','只查导线库，按计算拉断力降序列出前三个型号及其拉断力。',['83a5'],wire_table,[metric(w['name'],w['force'],'N') for w in leaders],[w['name'] for w in leaders],required_order=[w['name'] for w in leaders])
    add('C13','unit_conversion','只查导线库：外径列合计多少毫米？换算为米是多少？米保留4位小数。',['83a5'],wire_table,[metric('毫米',sum(w['diameter'] for w in wires),'mm'),metric('米',sum(w['diameter'] for w in wires)/1000,'m',4)])
    ground_table=[{'source':'3f12','sheet':'Sheet1'}]
    add('C14','key_value_mapping','只查接地电缆推荐表，分别列出315kVA、2000kVA对应的接地电缆截面，原样保留1*规格，不要交换对应关系。',['3f12'],ground_table,
        [],['315','1*50','2000','1*240'],expected_pairs=[['315','1*50'],['2000','1*240']])
    add('C15','threshold_and_percentage','接地电缆推荐表中容量>=1000kVA有多少条，占全部记录的百分之多少？比例2位小数。',['3f12'],ground_table,[metric('条数',5),metric('占比',Decimal(5)/13*100,'%',2)])
    batches=xml['d85a']['国网批次'];batchrows=range(3,101)
    feb=sum(batches.get(f'B{r}',{}).get('value')=='2月' for r in batchrows)
    mar=sum(batches.get(f'B{r}',{}).get('value')=='3月' for r in batchrows)
    add('C16','month_comparison','只查国网批次表：2月、3月各有多少条批次？3月减2月相差多少条？',['d85a'],[{'source':'d85a','sheet':'国网批次'}],[metric('2月',feb),metric('3月',mar),metric('差',mar-feb)])
    service=sum(batches.get(f'I{r}',{}).get('value')=='服务' for r in batchrows)
    material=sum(batches.get(f'I{r}',{}).get('value')=='物资' for r in batchrows)
    add('C17','category_ratio','只查国网批次表，I列采购类别为服务、物资各多少条？服务占全部记录的百分比多少？比例2位小数。',['d85a'],[{'source':'d85a','sheet':'国网批次'}],[metric('服务',service),metric('物资',material),metric('服务占比',Decimal(service)/98*100,'%',2)])
    add('C18','conflicting_source_reconciliation','只查损坏扫描报告的汇总统计：概览“其中疑似损坏”和类型分布中严重程度=疑似的数量合计分别是多少？列出类型分布减概览的差，解释来源不一致，不自行改写原值。',['damage'],
        [{'source':'damage','sheet':'汇总统计','region':'扫描概览'},{'source':'damage','sheet':'汇总统计','region':'损坏类型分布'}],
        [metric('概览',78),metric('分布',106),metric('差',28)],['不一致'])
    add('C19','reconciliation_and_percentage','重复文件报告汇总统计：复制件总数、其中找到原始、孤立复制件各是多少？后两项之和是否等于总数？找到原始占总数比例是多少，2位小数。',['duplicate'],
        [{'source':'duplicate','sheet':'汇总统计','region':'复制件/变体'}],
        [metric('总数',20778),metric('找到原始',20414),metric('孤立',364),metric('占比',Decimal(20414)/20778*100,'%',2)],['相等'])
    # Controlled CSV fixtures derive only non-personal technical fields from the real file.
    def fixture(key,filename,headers,records,provenance):
        path=OUT/'files'/filename
        with path.open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.writer(stream);writer.writerow(headers);writer.writerows(records)
        g['sources'][key]={'file':str(path.relative_to(OUT)).replace('\\','/'),'filename':filename,
            'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'expected_attachment_status':'ready',
            'kind':'controlled_derived_fixture','provenance':provenance}
    params=[[w['name'],str(w['diameter']),str(w['force'])] for w in wires]
    fixture('wire_params','导线参数_派生验收.csv',['导线型号','外径mm','计算拉断力N'],params,{'source':'83a5','sheet':'导线库','range':'A2:D7'})
    fixture('wire_missing','导线参数_故意缺项验收.csv',['导线型号','外径mm','计算拉断力N'],params[1:],
            {'source':'83a5','sheet':'导线库','range':'A3:D7','intentional_omission':'original A2 model omitted to test unmatched join'})
    selection=xml['83a5']['窄基塔-不带低压'];chosen=[]
    for row in range(2,14):
        model=selection.get(f'D{row}',{}).get('value');factor=selection.get(f'E{row}',{}).get('value')
        if model is not None and number(factor) is not None:chosen.append([row,model,factor])
    fixture('wire_selection','选型记录_派生验收.csv',['源行号','导线型号','安全系数'],chosen,
            {'source':'83a5','sheet':'窄基塔-不带低压','range':'D2:E13'})
    matches=sum(r[1] in {w['name'] for w in wires} for r in chosen)
    missing_count=sum(r[1]==wires[0]['name'] for r in chosen)
    add('C20','cross_workbook_join','按导线型号精确匹配选型记录与导线参数两个附件：列出选型总行数、匹配成功行数、未匹配行数。不按型号去重，不按行位置关联。',['wire_selection','wire_params'],
        [{'source':'wire_selection','sheet':'data'},{'source':'wire_params','sheet':'data'}],[metric('总行数',len(chosen)),metric('匹配',matches),metric('未匹配',len(chosen)-matches)])
    add('C21','join_missing_and_multiplicity','按导线型号精确匹配选型记录与故意缺项参数附件：列出未匹配记录行数、未匹配不同型号数及型号名称，保留重复选型记录。不要填补缺失参数。',['wire_selection','wire_missing'],
        [{'source':'wire_selection','sheet':'data'},{'source':'wire_missing','sheet':'data'}],[metric('未匹配记录',missing_count),metric('未匹配型号',1)],[wires[0]['name']])
    years={y:sum((total([r for r in rs if number(r['K'])==y]) for rs in rows.values()),Decimal(0)) for y in [2021,2022]}
    if not years[2021]:raise ValueError('No nonzero base for planned-year comparison')
    add('C22','year_comparison','排除汇总表，只按13区域表K列计划投产年份比较2021与2022：分别列出H列投资、2022减2021差额及增长率。金额4位、增长率2位小数。这是计划年份口径，不代表实际年度投资支出。',['dcb9'],tables(*rows),
        [metric('2021',years[2021],'万元',4),metric('2022',years[2022],'万元',4),metric('差',years[2022]-years[2021],'万元',4),metric('增长率',(years[2022]/years[2021]-1)*100,'%',2)])
    ab22={n:total([r for r in rows[n] if number(r['K'])==2022]) for n in ['市区','福清']}
    follow=[metric(n,v,'万元',4) for n,v in ab22.items()]+[metric('差',ab22['市区']-ab22['福清'],'万元',4)]
    add('T01','multi_turn','只比较市区与福清H列投资总额，并列出市区减福清差额，4位小数。',['dcb9'],tables('市区','福清'),ab[:3],['市区','福清'],conversation='T',turn=1)
    add('T02','multi_turn','沿用刚才这两个区域，口径改成K列=2022的记录；分别列出H列合计及差额，4位小数，不扩大区域范围。',['dcb9'],tables('市区','福清'),follow,['市区','福清'],conversation='T',turn=2)
    add('T03','multi_turn_report','把上一轮这两个区域2022年计划投产投资的结果生成HTML分析报告，列出双方金额及差额（4位小数），提供可下载PDF。保持刚才的筛选口径，不添加其他区域。',['dcb9'],tables('市区','福清'),follow,['市区','福清','2022'],conversation='T',turn=3,kind='report')
    add('R01','report_ground','生成接地电缆推荐表HTML分析报告，包含记录数、容量合计、容量最小和最大值及容量与截面对应表，提供PDF下载。不要合并其他附件。',['3f12'],ground_table,
        [metric('记录数',13),metric('容量合计',11605),metric('最小',160),metric('最大',2500)],['kVA'],kind='report')
    add('R02','report_conflict','只围绕损坏扫描报告的汇总统计生成HTML分析报告并提供PDF：列出总文件数、损坏/可疑文件数、损坏类型种数；对比疑似损坏的概览数与类型分布合计，列明差额，提示源数据是否一致，不改写。',['damage'],
        [{'source':'damage','sheet':'汇总统计','region':'扫描概览'},{'source':'damage','sheet':'汇总统计','region':'损坏类型分布'}],
        [metric('总文件',50571),metric('损坏/可疑',29431),metric('类型',29),metric('概览',78),metric('分布',106),metric('差',28)],['不一致'],kind='report')
    if len(suite)!=64:raise ValueError(f'Unexpected suite size {len(suite)}')
    (OUT/'gold.json').write_text(json.dumps(g,ensure_ascii=False,indent=2),encoding='utf-8')
    manifest={'version':'gold-v2','oracle':'direct source ZIP/XML and Decimal; no application parser/Agent output used',
              'review_status':'independent_computation_pending_business_signoff','human_reviewer':None,
              'source_rows':{n:[r['row'] for r in rs] for n,rs in rows.items()},'cases':suite,
              'dimensions':dict(Counter(q['dimension'] for q in suite)),
              'limits':['Automated screening is not semantic certification','Controlled cross-workbook fixtures are derivatives, not independent original business workbooks']}
    (OUT/'suite.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'questions':len(suite),'dimensions':manifest['dimensions'],'sources':len(g['sources']),'output':str(OUT)},ensure_ascii=False))


if __name__=='__main__':main()
