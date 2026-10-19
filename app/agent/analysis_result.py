"""One per-question contract, resolved against cached evidence before publication.

This validates execution/coverage, not the model's interpretation of a question
or arbitrary SQL predicates. No model-authored arithmetic or HTML is published.
"""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP, localcontext
import html
import json
import re

from .numeric_evidence import decimal_value, resolve_cell, calculate_weighted_average, computed_kinds


CONTRACT_PROMPT = '''
ANALYSIS RESULT CONTRACT (supersedes free-form terminate/report instructions):
After optional schema inspection, call define_analysis before querying:
{"tasks":[{"id":"city","label":"市区投资","operation":"value",
 "source":{"workbook_id":"...","table_name":"...","column":"column_h"},
 "scope":"exact requested filters; no extra exclusions","decimal_places":4},
 {"id":"fuqing","label":"福清投资","operation":"value","source":{...}},
 {"id":"delta","label":"市区减福清","operation":"subtract","inputs":["city","fuqing"],"decimal_places":4}]}
List EVERY requested metric, including overall totals, before executing. Use
value/table for SQL or cached calculation results. source names exact authorized
workbook/table/physical column; column is optional for counts. Keep units from
metadata, never treat blank identifier as an invalid business row by default.
Operations sum/average/count/weighted_average consume complete result rows via
bindings, or sum consumes inputs (earlier task IDs). subtract inputs=[LEFT,RIGHT]
means LEFT-RIGHT, never baseline/current. ratio inputs=[numerator,denominator].
growth_rate inputs=[baseline,current]. Inputs must name earlier tasks, not literals.
convert inputs=[one task ID], unit="万元" uses only known same-dimension unit
conversions (currency, length, area, mass, power); never supply a conversion factor.
For a combined task spanning tables/workbooks, use sources=[{workbook_id,
table_name,column},...] instead of source; every declared source must have evidence.
Use one task for EACH requested total; a per-region table does not replace an
overall comparison. For ratio/percent use ratio or percentage (ratio*100).
Tasks may set decimal_places 0..12. SQL calculations are approximate SQLite
values; use raw cells and program operations for exact arithmetic.
After queries, terminate with {"bindings":{"city":{"result_id":"result-1",
"row":0,"column":"total"},"fuqing":{"result_id":"result-2","row":0,"column":"total"}}}.
Bind EACH non-input task, omit computed tasks. No literal values or final prose.
For sum/average/count bind {"result_id":"...","column":"..."}; for table bind
{"result_id":"...","columns":["..."]}; weighted_average also needs value_column,
weight_column. Empty rows/NULL are handled by the server, never fabricate zero.
For a report call html_interpreter with the SAME bindings and optional title;
omit html. The server renders the same result for text and HTML. After report,
terminate with {}. The server supplies report/PDF links; never claim capabilities.
Original remarks: inspection notes are cached as inspection-N; table bindings
can read those note rows. Do not substitute samples for complete business data.
For genuinely ambiguous requests use define_analysis {"mode":"clarification",
"message":"specific question to clarify"}, then terminate {} (not a data answer).
For non-quantitative interpretation or missing-field explanations use
define_analysis {"mode":"explanation","message":"public explanation","evidence_ids":["inspection-1"]}
AFTER inspecting/querying evidence. It is labeled model interpretation, not a
verified numerical conclusion. Do not use this route instead of requested metrics.
Plan semantics/SQL filters still require human review; the contract proves only
coverage of declared tasks, cached provenance and deterministic computations.
You may replace the FULL plan with define_analysis until publication; retain all
requested tasks and reuse cached results. Fix missing units/operations rather than
retrying identical bindings. After publication the result is immutable.
SQL COUNT/SUM/AVG and aggregate_results already computed scalar metrics: use
operation=value with row/column, NEVER count/sum/average over those metrics.
Row operations consume only raw rows; grouped metrics must use value/table or
an explicit aggregate_results calculation. Unknown SQL expression units must be
declared as result_unit on the task (including compound units such as 万元·台).
Do not inherit a source column unit for a transformed SQL expression.
Optional summary is model interpretation with metric placeholders {{task:ID}};
use placeholders, not typed numeric conclusions. Include requested explanations
such as inconsistency/no-record findings. Review the user's requested outputs
before publication; a raw table is not a substitute for a requested computation.
'''


class AnalysisResult:
    def __init__(self, plan, catalogs):
        if not isinstance(plan, dict):
            raise ValueError('analysis plan must be an object')
        self.plan = deepcopy(plan)
        self.mode = plan.get('mode', 'analysis')
        self.catalogs = catalogs
        summary = plan.get('summary', '')
        if not isinstance(summary, str) or len(summary) > 8000:
            raise ValueError('summary must be bounded interpretation text')
        if self.mode in {'clarification', 'explanation'}:
            message = plan.get('message')
            if not isinstance(message, str) or not 3 <= len(message.strip()) <= 8000:
                raise ValueError('Provide a complete clarification/explanation message')
            if plan.get('tasks'):
                raise ValueError('Narrative mode cannot carry numeric tasks')
            if self.mode == 'explanation' and not plan.get('evidence_ids'):
                raise ValueError('Explanation requires inspected/query evidence IDs')
            self.tasks = []
            return
        if self.mode != 'analysis':
            raise ValueError('Unknown analysis mode')
        self.tasks = self.plan.get('tasks')
        if not isinstance(self.tasks, list) or not 1 <= len(self.tasks) <= 40:
            raise ValueError('Declare 1..40 requested tasks')
        seen = set()
        for task in self.tasks:
            if not isinstance(task, dict):
                raise ValueError('Task must be an object')
            key, label = task.get('id'), task.get('label')
            if not isinstance(key, str) or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,39}', key) or key in seen:
                raise ValueError('Task IDs must be unique short identifiers')
            if not isinstance(label, str) or not 1 <= len(label.strip()) <= 160:
                raise ValueError('Task label is required, at most 160 characters')
            operation = task.get('operation')
            if operation not in {'value', 'table', 'sum', 'average', 'count', 'subtract', 'ratio', 'percentage', 'growth_rate', 'weighted_average', 'convert'}:
                raise ValueError('Unsupported task operation')
            inputs = task.get('inputs', [])
            if not isinstance(inputs, list) or any(not isinstance(i, str) or i not in seen for i in inputs):
                raise ValueError('Inputs must reference earlier tasks; cycles and literals forbidden')
            if operation in {'subtract', 'ratio', 'percentage', 'growth_rate'} and len(inputs) != 2:
                raise ValueError('Binary operation requires two ordered task inputs')
            if operation == 'convert' and (len(inputs) != 1 or not isinstance(task.get('unit'), str)):
                raise ValueError('convert requires one task input and a target unit')
            if inputs and operation not in {'sum', 'subtract', 'ratio', 'percentage', 'growth_rate', 'convert'}:
                raise ValueError('This operation consumes cached rows, not task inputs')
            places = task.get('decimal_places')
            if places is not None and (type(places) is not int or not 0 <= places <= 12):
                raise ValueError('decimal_places must be an integer 0..12')
            if not isinstance(task.get('scope', ''), str) or len(task.get('scope', '')) > 1000:
                raise ValueError('Task scope must be bounded text')
            if not inputs:
                self._source(task)
            seen.add(key)
        if any(key not in seen for key in re.findall(r'\{\{task:([^}]+)\}\}', summary)):
            raise ValueError('Summary references unknown task IDs')

    def _source(self, task):
        if task.get('sources') is not None:
            if task.get('source') is not None or not isinstance(task['sources'], list) or not 1 <= len(task['sources']) <= 40:
                raise ValueError('Use source or a bounded sources list, not both')
            sources = [self._source(dict(task, source=s, sources=None)) for s in task['sources']]
            units = {s['unit'] for s in sources}
            if len(units) > 1:
                raise ValueError('Task sources have incompatible units')
            return {'sources': sources, 'unit': sources[0]['unit'],
                    'display_name': '、'.join(dict.fromkeys(s['display_name'] for s in sources)),
                    'table_label': '、'.join(dict.fromkeys(s['table_label'] for s in sources))}
        source = task.get('source')
        if not isinstance(source, dict):
            raise ValueError('Task '+task['id']+' requires an authorized source')
        book = next((b for b in self.catalogs if b['workbook_id'] == source.get('workbook_id')), None)
        table = next((t for t in (book or {}).get('catalog', {}).get('tables', [])
                      if t['table_name'] == source.get('table_name')), None)
        if table is None:
            raise ValueError('Unknown workbook/table in task '+task['id'])
        column = source.get('column')
        field = next((c for c in physical_columns(table) if c.get('physical_name') == column), None)
        if column and field is None:
            raise ValueError('Unknown physical column in task '+task['id'])
        unit = metadata_unit(field or {})
        declared = task.get('unit', '')
        if not isinstance(declared, str) or len(declared) > 40:
            raise ValueError('Unit must be short text')
        if unit and declared and declared != unit and task['operation'] != 'count':
            raise ValueError('Declared unit contradicts source metadata')
        return {'workbook_id': source['workbook_id'], 'table_name': source['table_name'], 'column': column,
                'display_name': book.get('display_name') or book.get('source_file') or '工作簿',
                'table_label': table.get('display_name') or table['table_name'],
                'unit': '条' if task['operation'] == 'count' else unit or declared,
                'unit_basis': 'metadata' if unit else 'plan' if declared else 'unspecified'}

    def _evidence(self, binding, results, task):
        if not isinstance(binding, dict) or set(binding)-{'result_id', 'row', 'column', 'columns', 'value_column', 'weight_column'}:
            raise ValueError('Bindings accept cached coordinates only, never literal values')
        evidence = next((r for r in results if r.get('result_id') == binding.get('result_id')), None)
        if evidence is None or evidence.get('error') or evidence.get('truncated'):
            raise ValueError('Missing, failed or truncated evidence for '+task['id'])
        source = self._source(task)
        # Aggregates retain transitive input IDs; validate every leaf, not just a matching number.
        def leaves(item, visited):
            key = item.get('result_id')
            if key in visited:
                raise ValueError('Cyclic evidence provenance')
            if item.get('input_result_ids'):
                found = []
                for parent in item['input_result_ids']:
                    raw = next((r for r in results if r.get('result_id') == parent), None)
                    if raw is None or raw.get('truncated') or raw.get('error'):
                        raise ValueError('Incomplete input provenance')
                    found.extend(leaves(raw, visited | {key}))
                return found
            return [item]
        import sqlglot
        from sqlglot import exp
        declared = source.get('sources', [source])
        covered = set()
        for raw in leaves(evidence, set()):
            if raw.get('inspection_table'):
                tables = {raw['inspection_table']}
            else:
                try:
                    tables = {t.name for t in sqlglot.parse_one(raw.get('sql', ''), read='sqlite').find_all(exp.Table)}
                except (sqlglot.errors.ParseError, AttributeError):
                    raise ValueError('Evidence lacks query provenance')
            matching = {(s['workbook_id'], s['table_name']) for s in declared
                        if s['workbook_id'] == (raw.get('source') or {}).get('workbook_id') and s['table_name'] in tables}
            if not matching:
                raise ValueError('Evidence workbook/table differs from declared task source')
            covered.update(matching)
        if covered != {(s['workbook_id'], s['table_name']) for s in declared}:
            raise ValueError('Not all declared task sources were queried')
        return evidence, source

    def resolve(self, bindings, results):
        if self.mode != 'analysis':
            refs = self.plan.get('evidence_ids', [])
            if not isinstance(refs, list) or any(not isinstance(i, str) for i in refs):
                raise ValueError('Evidence IDs must be a list of strings')
            if any(not any(r.get('result_id') == i and not r.get('error') for r in results) for i in refs):
                raise ValueError('Explanation evidence is unavailable')
            return {'mode': self.mode, 'message': self.plan['message'], 'tasks': [], 'evidence_ids': refs}
        if not isinstance(bindings, dict):
            raise ValueError('Bindings must map every source task ID to cached evidence')
        required = {t['id'] for t in self.tasks if not t.get('inputs')}
        if set(bindings) != required:
            raise ValueError('Missing/unknown task bindings: '+', '.join(sorted(required ^ set(bindings))))
        resolved, values = [], {}
        with localcontext() as ctx:
            ctx.prec = 128
            for task in self.tasks:
                key, op = task['id'], task['operation']
                item = {'id': key, 'label': task['label'], 'operation': op, 'scope': task.get('scope', ''), 'status': 'ok'}
                inputs = [values[i] for i in task.get('inputs', [])]
                if inputs:
                    if any(i['operation'] == 'table' for i in inputs):
                        raise ValueError('Scalar operation cannot consume tables')
                    units = {i['unit'] for i in inputs}
                    if len(units) > 1 and op != 'ratio':
                        raise ValueError('Incompatible units in '+key)
                    item['unit'] = '%' if op in {'growth_rate', 'percentage'} else (
                        divide_units(inputs[0]['unit'], inputs[1]['unit']) if op == 'ratio' else next(iter(units), ''))
                    item['inputs'] = list(task['inputs'])
                    if op == 'convert':
                        factor = _conversion(inputs[0]['unit'], task['unit'])
                        item['unit'] = task['unit']
                    item['formula'] = (inputs[0]['label']+'：'+inputs[0]['unit']+' → '+task['unit'] if op == 'convert' else
                                       ' − '.join(i['label'] for i in inputs) if op == 'subtract' else
                                       ' + '.join(i['label'] for i in inputs) if op == 'sum' else
                                       '('+inputs[1]['label']+' − '+inputs[0]['label']+') ÷ '+inputs[0]['label']+' × 100%' if op == 'growth_rate' else
                                       inputs[0]['label']+' ÷ '+inputs[1]['label']+(' × 100%' if op == 'percentage' else ''))
                    if any(i['status'] != 'ok' for i in inputs):
                        value = None
                        item['status'] = 'missing_input'
                    else:
                        nums = [decimal_value(i['_raw']) for i in inputs]
                        if op == 'sum':
                            value = sum(nums, Decimal(0))
                        elif op == 'subtract':
                            value = nums[0]-nums[1]
                        elif op == 'convert':
                            value = nums[0]*factor
                        else:
                            denominator = nums[0] if op == 'growth_rate' else nums[1]
                            value = None if denominator == 0 else ((nums[1]-nums[0])/denominator*100 if op == 'growth_rate' else nums[0]/denominator*(100 if op == 'percentage' else 1))
                            if value is None:
                                item['status'] = 'undefined'
                else:
                    binding = bindings[key]
                    evidence, source = self._evidence(binding, results, task)
                    item.update(source=source, unit=source['unit'], binding=deepcopy(binding))
                    rows = evidence.get('rows') or []
                    columns = evidence.get('columns') or []
                    kind = result_kind(evidence, binding.get('column'))
                    item['input_kind'] = kind
                    if op in {'count', 'sum', 'average', 'weighted_average'} and kind != 'rows':
                        raise ValueError('Task '+key+': evidence is already computed; use value with row/column, not '+op)
                    if kind == 'computed' and op != 'count':
                        declared_unit = task.get('result_unit')
                        if declared_unit is not None:
                            if not isinstance(declared_unit, str) or len(declared_unit) > 40:
                                raise ValueError('result_unit must be short text')
                            item['unit'] = declared_unit
                            item['unit_basis'] = 'model_expression_declaration'
                        elif transformed_projection(evidence, binding.get('column')):
                            raise ValueError('Task '+key+': SQL expression unit unknown; declare result_unit or use raw rows')
                    if any(not isinstance(r, (list, tuple)) or len(r) != len(columns) for r in rows):
                        raise ValueError('Evidence rows do not match their schema')
                    if op == 'table':
                        selected = binding.get('columns', columns)
                        if not isinstance(selected, list) or not selected or len(selected) > 100 or any(columns.count(c) != 1 for c in selected):
                            raise ValueError('Select unambiguous table columns')
                        item['columns'] = selected
                        value = [[row[columns.index(c)] for c in selected] for row in rows]
                        if not rows:
                            item['status'] = 'no_records'
                    elif op == 'count':
                        value = len(rows)
                    elif op == 'weighted_average':
                        calculation = calculate_weighted_average([evidence], dict(binding, result_ids=[evidence['result_id']]))
                        value = calculation['rows'][0][2]
                        if value is None:
                            item['status'] = 'undefined'
                    elif op in {'sum', 'average'}:
                        column = binding.get('column')
                        if columns.count(column) != 1:
                            raise ValueError('Unknown aggregate input column')
                        nums = [decimal_value(r[columns.index(column)]) for r in rows if r[columns.index(column)] is not None]
                        value = sum(nums, Decimal(0)) if nums else None
                        if op == 'average' and nums:
                            value /= len(nums)
                        if value is None:
                            item['status'] = 'no_records' if not rows else 'null'
                    elif not rows:
                        # Validate schema even when no row exists; empty is not a fabricated zero.
                        if columns.count(binding.get('column')) != 1 or type(binding.get('row')) is not int or binding['row'] < 0:
                            raise ValueError('Empty result still requires valid column/row coordinates')
                        value, item['status'] = None, 'no_records'
                    else:
                        value, _ = resolve_cell(results, binding)
                        if isinstance(value, str) and task.get('decimal_places') is not None and not computed_kinds(evidence, binding.get('column')):
                            raise ValueError('Original text cannot be rounded; preserve identifiers and specifications')
                        if value is None:
                            item['status'] = 'null'
                item['_raw'] = value
                places = task.get('decimal_places')
                if op == 'table':
                    item['rows'] = value
                    item['value'] = None
                elif value is None:
                    item['value'] = None
                elif places is not None:
                    item['value'] = format(decimal_value(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP), 'f')
                else:
                    if isinstance(value, Decimal) and value.as_tuple().exponent < -12:
                        item['value'] = format(value.quantize(Decimal('0.000000000001')), 'f').rstrip('0').rstrip('.')
                    else:
                        item['value'] = format(value, 'f') if isinstance(value, Decimal) else str(value)
                values[key] = item
                resolved.append(item)
        packet = {'mode': 'analysis', 'tasks': [{k: v for k, v in i.items() if k != '_raw'} for i in resolved],
                'validation_scope': 'declared_task_coverage_provenance_and_computation',
                'limitations': '计划及SQL条件由模型解释；验证不证明业务口径必然正确。'}
        summary = self.plan.get('summary', '')
        if summary:
            packet['interpretation'] = re.sub(r'\{\{task:([^}]+)\}\}',
                lambda m: _display(values[m[1]])+(' '+values[m[1]]['unit'] if values[m[1]]['status'] == 'ok' else ''), summary)
        if len(json.dumps(packet, ensure_ascii=False)) > 200_000:
            raise ValueError('Analysis result is too large; select requested columns or aggregate in SQL')
        return packet

    def text(self, packet):
        if packet['mode'] != 'analysis':
            prefix = '需要澄清：' if packet['mode'] == 'clarification' else '分析说明（模型解释）：'
            return prefix+packet['message']
        lines = []
        for item in packet['tasks']:
            lines.append('### '+_md(item['label']))
            if item['operation'] == 'table' and item['status'] == 'ok':
                table = ['| '+' | '.join(_md(c) for c in item['columns'])+' |',
                         '| '+' | '.join('---' for _ in item['columns'])+' |']
                table.extend('| '+' | '.join(_md('空值（NULL）' if v is None else v) for v in row)+' |' for row in item['rows'])
                lines.append('\n'.join(table))
            else:
                lines.append(_md(_display(item))+(' '+_md(item['unit']) if item['status'] == 'ok' and item['unit'] else ''))
            if item.get('formula'):
                lines.append('计算：'+_md(item['formula']))
            if item.get('scope'):
                lines.append('口径：'+_md(item['scope']))
            if item.get('source'):
                lines.append('来源：'+_md(item['source']['display_name'])+' · '+_md(item['source']['table_label']))
            lines.append('')
        if packet.get('interpretation'):
            lines.append('分析说明（模型解释）：'+_md(packet['interpretation']))
        return '\n\n'.join(lines).strip()

    def html(self, packet, title='分析报告'):
        esc = lambda x: html.escape(str(x))
        body = ['<h1>'+esc(title)+'</h1>']
        if packet.get('interpretation'):
            body.append('<p>分析说明（模型解释）：'+esc(packet['interpretation'])+'</p>')
        if packet['mode'] != 'analysis':
            body.append('<p>'+esc(self.text(packet))+'</p>')
        for item in packet['tasks']:
            body.append('<section><h2>'+esc(item['label'])+'</h2>')
            if item['operation'] == 'table' and item['status'] == 'ok':
                body.append('<table><thead><tr>'+''.join('<th>'+esc(c)+'</th>' for c in item['columns'])+'</tr></thead><tbody>')
                body.extend('<tr>'+''.join('<td>'+esc('NULL' if v is None else v)+'</td>' for v in row)+'</tr>' for row in item['rows'])
                body.append('</tbody></table>')
            else:
                body.append('<p>'+esc(_display(item))+(' '+esc(item['unit']) if item['status'] == 'ok' else '')+'</p>')
            for label, value in [('计算', item.get('formula')), ('口径', item.get('scope')),
                                 ('来源', (item.get('source') or {}).get('display_name', '')+' · '+(item.get('source') or {}).get('table_label', ''))]:
                if value and value != ' · ':
                    body.append('<p class="note">'+label+'：'+esc(value)+'</p>')
            body.append('</section>')
        return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>'+esc(title)+'</title><style>body{font-family:sans-serif;line-height:1.6;color:#243447;margin:24px}h1{font-size:24px}h2{font-size:18px;break-after:avoid}p{margin:6px 0}.note{font-size:12px;color:#586575}table{border-collapse:collapse;width:100%;font-size:12px;table-layout:auto}td,th{border:1px solid #ddd;padding:6px;overflow-wrap:anywhere}thead{display:table-header-group}tr{break-inside:avoid}</style></head><body>'+''.join(body)+'</body></html>'


def _display(item):
    return {'no_records': '未查到记录', 'null': '空值（NULL），未视为零',
            'undefined': '未定义（分母为零）', 'missing_input': '输入缺失，无法计算'}.get(item['status'], item.get('value') or '')


def result_kind(evidence, column=None):
    """Computed metrics cannot be silently reinterpreted as business rows."""
    if evidence.get('operation'):
        return 'computed'
    import sqlglot
    from sqlglot import exp
    try:
        query = sqlglot.parse_one(evidence.get('sql') or '', read='sqlite')
        # Includes nested/CTE aggregation: require an explicit value/table binding.
        if query.find(exp.AggFunc) or query.find(exp.Group) or query.find(exp.Window):
            return 'computed'
        if column and computed_kinds(evidence, column):
            return 'computed'
        return 'rows'
    except (sqlglot.errors.ParseError, AttributeError):
        return 'unknown' if not evidence.get('inspection_table') else 'rows'


def transformed_projection(evidence, column):
    """Only direct columns and unit-preserving aggregates inherit source units."""
    if evidence.get('operation'):
        return (evidence['operation'] in {'ratio', 'growth_rate', 'match_keys'}
                or evidence['operation'] == 'weighted_average' and column != 'weighted_average')
    if not column:
        return False
    import sqlglot
    from sqlglot import exp
    try:
        query = sqlglot.parse_one(evidence.get('sql') or '', read='sqlite')
        projection = query.selects[evidence['columns'].index(column)]
        if query.find(exp.CTE) or query.find(exp.Subquery) or isinstance(query, exp.SetOperation):
            # No guessed lineage through nested queries: request an explicit unit.
            return True
        return any(projection.find(t) for t in (exp.Mul, exp.Div, exp.Add, exp.Sub, exp.Count))
    except (ValueError, IndexError, AttributeError, sqlglot.errors.ParseError):
        return True


def divide_units(numerator, denominator):
    """Cancel identical declared factors only; never infer a business dimension."""
    if not numerator or not denominator:
        return ''
    from collections import Counter
    def factors(unit):
        parts = unit.split('/')
        if len(parts) > 2:
            raise ValueError('Use a unit with one explicit division')
        return (parts[0].split('·'), parts[1].split('·') if len(parts) == 2 else [])
    n_top, n_bottom = factors(numerator)
    d_top, d_bottom = factors(denominator)
    top, bottom = Counter(n_top+d_bottom), Counter(n_bottom+d_top)
    cancelled = top & bottom
    top, bottom = top-cancelled, bottom-cancelled
    if not top and not bottom:
        return '倍'
    return ('·'.join(top.elements()) or '1')+('/'+'·'.join(bottom.elements()) if bottom else '')


def _md(value):
    text = re.sub(r'([\\`*_\[\]<>&])', r'\\\1', str(value))
    return text.replace('|', '\\|').replace('\n', '<br>')


def _conversion(source, target):
    # ponytail: fixed physical units, not a symbolic algebra/unit dependency.
    dimensions = [
        {'元': 0, '千元': 3, '万元': 4, '亿元': 8},
        {'mm': -3, '毫米': -3, 'cm': -2, '厘米': -2, 'm': 0, '米': 0, 'km': 3, '千米': 3},
        {'mm²': -6, 'mm2': -6, 'm²': 0, 'm2': 0, '平方米': 0},
        {'g': -3, '克': -3, 'kg': 0, '千克': 0, 't': 3, '吨': 3},
        {'W': 0, 'kW': 3, 'MW': 6}, {'VA': 0, 'kVA': 3, 'MVA': 6}]
    for units in dimensions:
        if source in units and target in units:
            return Decimal(10)**(units[source]-units[target])
    raise ValueError('Unknown/incompatible unit conversion; do not guess a factor')


def physical_columns(table):
    """The same physical schema for inspection and contract source validation."""
    columns = []
    for c in table.get('columns', []):
        if c.get('mixed_numeric'):
            for suffix, sql_type in (('_value', 'REAL'), ('_raw', 'TEXT')):
                columns.append({'physical_name': str(c.get('physical_name'))+suffix,
                                'display_name': c.get('display_name'), 'sql_type': sql_type,
                                'logical_name': c.get('physical_name'), 'mixed_numeric': True,
                                **{k: c.get(k) for k in ('semantic_type', 'unit_hint', 'number_formats')}})
        else:
            columns.append({k: c.get(k) for k in ('physical_name', 'display_name', 'sql_type', 'semantic_type', 'unit_hint', 'number_formats')})
    return columns


def metadata_unit(field):
    # Existing parser stores the whole header in unit_hint, not a canonical unit.
    unit = r'(?:万元|亿元|千元|元|条|项|个|台|件|批|吨|千克|克|米|毫米|厘米|千米|平方米|kVA|MVA|VA|kW|MW|W|kV|V|mm[²2]?|cm|km|m[²2]?|kg|g|t|N|%)'
    pattern = unit+r'(?:[/·]'+unit+r')*'
    for hint in (field.get('unit_hint'), field.get('display_name')):
        text = str(hint or '').strip()
        candidates = [text]+list(reversed(re.findall(r'[（(]([^()（）]+)[）)]', text)))
        for candidate in candidates:
            if re.fullmatch(pattern, candidate):
                return candidate
    return ''
