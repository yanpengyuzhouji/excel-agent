import fs from 'node:fs/promises';
import path from 'node:path';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';

const root = path.resolve('data/business_acceptance/numeric-gate-v1');
await fs.mkdir(root, {recursive:true});
// Synthetic engineering fixture only; no business/customer data.
const ledger = [
  ['id','year','region','item','amount_yuan','quantity','unit_price_yuan','stored_ratio','record_date','asset_code','spec','model','remark'],
  ['00001',2024,'北','设备甲',100.10,2,50.05,.05,new Date('2024-01-15T00:00:00Z'),'9007199254740993','1*35','Qwen3.8-27B','型号Qwen3.8-27B，电压10kV，标准GB/T 12345-2025。'],
  ['00002',2024,'北','设备乙',200.20,4,50.05,.25,new Date('2024-02-20T00:00:00Z'),'9007199254740994','DN100','Model-v1.2.3','合计为历史原文，金额不是统计结果：2025元。'],
  ['00003',2024,'南','设备丙',300.30,3,100.10,.35,new Date('2024-03-01T00:00:00Z'),'9007199254740995','2*70','Model-v2','平均只是一段标签，版本v1.2.3，更新时间2025/03/01。'],
  ['00004',2024,'南','设备丁',-50.60,1,-50.60,.05,new Date('2024-04-01T00:00:00Z'),'9007199254740996','DN80','Model-v2','说明：编号00004无需计算。'],
  ['00005',2025,'北','设备甲',150.15,3,50.05,.05,new Date('2025-01-15T00:00:00Z'),'9007199254740997','1*35','Model-v3','规格1*35 mm2，比例5%，请原样保留。'],
  ['00006',2025,'北','设备乙',250.25,5,50.05,.25,new Date('2025-02-20T00:00:00Z'),'9007199254740998','DN100','Model-v3','备注：缺失值NULL非0。'],
  ['00007',2025,'南','设备丙',0,0,88,0,new Date('2025-03-01T00:00:00Z'),'9007199254740999','2*70','Model-v3','不应把SUM字样当作求和请求。'],
  ['00008',2025,'南','设备丁',49.60,2,24.80,.5,new Date('2025-04-01T00:00:00Z'),'9007199254741000','DN80','Model-v4','数据来源合成测试，生成日期2026-10-16。'],
];
const comparisonInputs = [
  ['scenario','baseline_yuan','current_yuan','unit'],
  ['正常',100,120,'元'],['零基期',0,50,'元'],['双零',0,0,'元'],
  ['负基期',-100,-50,'元'],['缺基期',null,50,'元'],['小数',.1,.3,'元'],
  ['大额精度',1000000000000.01,1000000000000.02,'元'],
  ['四舍五入',1,1.005,'元'],['下降',100,80,'元'],
];
const comparisons = [
  ['场景','基期金额（元）','本期金额（元）'],
  ...comparisonInputs.slice(1).map(row=>row.slice(0,3)),
];
const snapshots = [
  ['key','avg_metric','total_metric','rounding_value','note'],
  ...Array.from({length:8},(_,i)=>['snapshot-'+String.fromCharCode(65+i),7.5+i,123.45+i,1.005+i,'平均值与总额是原始字段，不要求重新计算。']),
];
const wb = Workbook.create();
const sheets = [['交易明细',ledger],['比较场景',comparisons],['指标快照',snapshots]];
for (const [name, rows] of sheets) {
  const sh=wb.worksheets.add(name);
  sh.showGridLines=false;
  sh.getRangeByIndexes(0,0,rows.length,rows[0].length).values=rows;
  const range=sh.getRangeByIndexes(0,0,rows.length,rows[0].length);
  range.format.font={name:'Arial',size:10};
  range.format.columnWidth=20;
  range.format.rowHeight=22;
  sh.getRangeByIndexes(0,0,1,rows[0].length).format={fill:'#24364B',font:{name:'Arial',size:10,bold:true,color:'#FFFFFF'},rowHeight:28};
  sh.freezePanes.freezeRows(1);
  if(name==='交易明细') {
    sh.getRange('A2:A9').setNumberFormat('@');
    sh.getRange('J2:J9').setNumberFormat('@');
    sh.getRange('E2:E9').setNumberFormat('0.00');
    sh.getRange('G2:G9').setNumberFormat('0.00');
    sh.getRange('H2:H9').setNumberFormat('0.00%');
    sh.getRange('I2:I9').setNumberFormat('yyyy-mm-dd');
    sh.getRange('M1:M9').format.columnWidth=78;
    sh.getRange('A10').values=[['备注：合计3类设备；规格1*35 mm2；2025/03/01仅为说明日期，不是明细。']];
  } else {
    sh.getRangeByIndexes(1,1,rows.length-1,Math.min(3,rows[0].length-1)).setNumberFormat('0.00##########');
    sh.getRange('B:D').format.columnWidth=30;
    if(name==='指标快照') sh.getRange('E1:E9').format.columnWidth=65;
  }
}
wb.recalculate();
const inspection=await wb.inspect({kind:'region',sheetId:'交易明细',range:'A1:M10',maxChars:5000,tableMaxRows:10,tableMaxCols:13});
await fs.writeFile(path.join(root,'fixture-inspection.ndjson'),inspection.ndjson);
for (const [name,range] of [['交易明细','A1:G9'],['比较场景','A1:C10'],['指标快照','A1:E9']]) {
  const preview=await wb.render({sheetName:name,range,scale:1,format:'png'});
  await fs.writeFile(path.join(root,name+'.png'),new Uint8Array(await preview.arrayBuffer()));
}
const artifact=await SpreadsheetFile.exportXlsx(wb);
await artifact.save(path.join(root,'数字门禁_v1.2_2025.xlsx'));
await fs.writeFile(path.join(root,'fixture.json'),JSON.stringify({synthetic:true,source:'Synthetic numeric validation fixture; no real business data',sheets:Object.fromEntries(sheets)},null,2));
console.log(JSON.stringify({output:root,sheets:sheets.map(([name,rows])=>({name,rows:rows.length-1}))}));
