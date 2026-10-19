"""Separate semantic observations and lifecycle checks from immutable baseline results."""
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys
import httpx

folder=Path(sys.argv[1]).resolve()
r=json.loads((folder/'acceptance.json').read_text(encoding='utf-8'))
audit=json.loads((folder/'execution-audit.json').read_text(encoding='utf-8'))
semantic=[
 {'id':'C01','verdict':'presentation_warning','note':'Amounts and ratio are correct, but the final answer omits the monetary unit.'},
 {'id':'C06','verdict':'semantic_defect','note':'The weighted average is numerically correct. SUM(H*N)/SUM(N) has the unit of H (万元), not 万元/台 as stated in the answer.'},
 {'id':'C10','verdict':'correct_refusal_with_coverage_warning','note':'Correctly refuses actual-collection YoY and does not substitute investment. Claims about absent months across the workbook exceed the shown single-table coverage.'},
 {'id':'C12','verdict':'screening_false_negative','note':'Top-three models, descending order and forces are correct. Space-separated thousands and a full-width colon caused strict text/numeric mismatch. Initial result is retained.'},
 {'id':'C19','verdict':'correct_metrics_with_inference_warning','note':'Reconciliation and percentage are correct; equality of totals alone does not establish no overlap or omission of underlying records.'},
 {'id':'R01','verdict':'report_functional_pass_with_print_warning','note':'HTML preview, PDF download and both PDF pages inspected. Print CSS hides PDF instructions but leaves an empty export-PDF heading.'},
 {'id':'R02','verdict':'report_functional_and_visual_pass','note':'HTML preview and all three PDF pages inspected; required metrics and source contradictions preserved; tables readable.'},
 {'id':'T03','verdict':'report_functional_pass_with_print_warning','note':'2022 and two-region scope retained; preview, chart and both PDF pages inspected. An empty export-PDF heading remains in print; answer recommends browser printing instead of the available direct PDF download.'},
]
life={'source_imports':[],'report_artifacts':[]}
with httpx.Client(base_url='http://127.0.0.1:8092/api/v1/',timeout=60,trust_env=False) as c:
    catalog=c.get('data-catalog');catalog.raise_for_status();ids={v['workbook_id'] for v in catalog.json()}
    for key,source in r['prepared_sources'].items():
        a=source['attachment'];status=c.get('workbook-imports/'+a['import_id']).status_code
        life['source_imports'].append({'source':key,'import_http_status':status,'absent_from_catalog':a['workbook_id'] not in ids,'passed':status==404 and a['workbook_id'] not in ids})
    for q in r['questions']:
        if not q.get('html'):continue
        url='http://127.0.0.1:8092'+q['html']['url']
        html=c.get(url).status_code;pdf=c.get(url+'/pdf').status_code
        life['report_artifacts'].append({'id':q['id'],'html_http_status':html,'pdf_http_status':pdf,'passed':html==404 and pdf==404})
life['passed']=all(v['passed'] for k in ('source_imports','report_artifacts') for v in life[k]) and all(v['passed'] for v in r['cleanup'])
times=[q['elapsed_seconds'] for q in r['questions']]
errors=[{'id':q['id'],'error':q.get('error') or q.get('exception')} for q in r['questions'] if q['status']=='execution_error']
groups={}
for key,predicate in [('原基础题',lambda q:q['dimension']=='basic'),('新增复杂题',lambda q:q['id'].startswith('C')),('连续追问',lambda q:q['id'].startswith('T')),('独立报告题',lambda q:q['id'].startswith('R'))]:
    qs=[q for q in r['questions'] if predicate(q)]
    groups[key]={'total':len(qs),'statuses':dict(Counter(q['status'] for q in qs))}
delivery={'created_at':datetime.now(timezone.utc).isoformat(),'groups':groups,'semantic_observations':semantic,'lifecycle':life,
 'latency_seconds':{'median':statistics.median(times),'p95':sorted(times)[int(.95*(len(times)-1))],'maximum':max(times)},
 'execution_errors':errors,'same_image':r['image_before']==r['image_after'],'business_signoff':False,
 'conclusion':'Not a clean acceptance: preserve first-attempt failures, semantic unit defect and presentation warnings. Suitable for controlled pilot, not an unconditional production pass.'}
(folder/'delivery.json').write_text(json.dumps(delivery,ensure_ascii=False,indent=2),encoding='utf-8')
lines=['# 黄金测试集 v2：端到端回归验收报告','',
 f"执行时间：{r['started_at']} 至 {r['finished_at']}。模型：DeepSeek Flash（配置名 {r['model']}）。",
 f"共执行 {len(r['questions'])}/64 轮；全程同一容器镜像：{delivery['same_image']}。未用重试覆盖首次失败。",'',
 '## 结果','',
 '| 分组 | 轮数 | 自动筛查通过 | 执行失败 | 待复核 |','|---|---:|---:|---:|---:|']
for key,g in groups.items():
    s=g['statuses'];lines.append(f"| {key} | {g['total']} | {s.get('auto_pass_pending_semantic_review',0)} | {s.get('execution_error',0)} | {s.get('needs_review',0)} |")
lines += ['', '原始合计：59轮自动筛查通过、4轮执行失败、1轮待复核。C12已复核为格式误报；不得据此忽略C06的单位错误。自动筛查通过不等于业务语义认证。',
 '', '导入与接口：9/9附件通过；4/4不支持格式按预期拒绝。包含真实复杂汇总页、多层表头及3个明确标注的派生CSV。派生CSV不算新增独立业务样本。',
 '', '报告：R01、R02、T03均完成HTML生成、关键数值核验、PDF文字提取、真实历史会话预览、浏览器PDF下载；默认收起、浏览器无脚本错误。PDF分别为2、3、2页，均逐页视觉检查。连续追问正确保留两个区域及2022年条件。',
 '', '## 已发现的问题','',
 '1. G006-Q2、G017-Q2、C02：模型某次调用10秒未返回正文而终止；不是解析失败。建议区分用户首个进度反馈时限与每次模型调用的正文超时。',
 '2. G016-Q1：必需上下文超出窗口。轨迹含一次约315KB的导入清单查询结果，且后续持续观察至第34轮。应限制元数据/单字段回传体积，保留紧凑的表映射与已完成证据，防止压缩后反复观察。此为轨迹支持的风险机制，仍需修复时做因果验证。',
 '3. C06：加权平均176.7987数值正确，单位应为万元，回答写成万元/台，属于真实语义错误。C01金额未标注单位。',
 '4. C12：排名与数值正确，空格千分位及全角冒号触发严格评测误报；后续评测器已补齐格式规范化，单独补充检查通过，未重跑模型、未覆盖本轮原始判定。评测器7项合成检查通过，并增加拒答内容检查。',
 '5. 部分简单题查询过多，回答暴露内部表名和版本；C10缺失数据说明范围过大，C19由总量相等推导无重叠的表述证据不足。',
 '6. R01、T03打印样式留下空的“导出PDF”标题；部分报告回答仍建议浏览器打印，没有引导现成的直接下载按钮。属于展示问题，不是报告不可用。',
 '', '## 性能、清理与范围','',
 f"单轮中位耗时 {delivery['latency_seconds']['median']:.2f} 秒，P95 {delivery['latency_seconds']['p95']:.2f} 秒，最大 {delivery['latency_seconds']['maximum']:.2f} 秒（报告轮含PDF及浏览器验收时间）。重复文件整文件导入233.391秒，为明显性能风险。",
 f"本轮72个临时会话均清理成功。9个导入均返回404且退出目录，3份在线报告及PDF均返回404；本地私有验收副本保留。生命周期检查：{life['passed']}。未删除用户原有会话。",
 '', '当前结论：本轮不是全绿验收。基础解析与报告链路可用于受控试用；先处理正文超时策略、上下文膨胀和单位校验，再做失败题及相关复杂题复测，不建议无条件正式放行。黄金预期仍待业务负责人签字；不包含并发压测、故障恢复或安全认证。',
 '', '## 证据文件','',
 '- acceptance.json：首次完整回归结果、镜像、接口及清理记录。',
 '- execution-audit.json：逐题查询次数、来源与精确表匹配、重复SQL及压缩记录。',
 '- delivery.json：语义观察、延迟和删除后生命周期检查。',
 '- 各题目录：题面、原始SSE事件、结果；报告题另有HTML、PDF、逐页图片与真实预览截图。',
 '', '真实文件、标准答案、SSE及报告均保存在Git/Docker忽略的私有目录，不对外提交。']
(folder/'回归验收报告.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print(json.dumps({'groups':groups,'lifecycle':life['passed'],'latency':delivery['latency_seconds'],'report':str(folder/'回归验收报告.md')},ensure_ascii=False))
