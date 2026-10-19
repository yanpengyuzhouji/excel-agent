"""Exact arithmetic and optional cached-cell reference rendering.

References bind values to result/row/column, not just matching numbers anywhere
in the evidence. SQL scope and business interpretation still require review.
"""
import re
import html
from decimal import Decimal, InvalidOperation, Inexact, ROUND_HALF_UP, localcontext
from html.parser import HTMLParser


DERIVED = re.compile(r'汇总(?!表)|总额|总计|合计|总投资|差额|相差|差值|增长|增幅|增减|同比|环比|占比|均值|平均|加权|求和|\b(?:sum|total|difference|growth|average|ratio)\b', re.I)
REFERENCE = re.compile(r'\{\{((?:result|aggregate|inspection)-\d+):(\d+):([^{}:|]+?)(?:\\?\|(\d{1,2}))?\}\}')
NUMBER = re.compile(r'[-+]?\d+(?:,\d{3})*(?:\.\d+)?(?:[eE][-+]?\d+)?')
CHINESE_NUMBER = re.compile(r'百分之[零〇一二两三四五六七八九十百千万亿点]+|[零〇一二两三四五六七八九十百][零〇一二两三四五六七八九十百千万亿点]*(?:万元|亿元|元|项|条|倍|个百分点)')


def decimal_value(value):
    if isinstance(value, bool) or value is None or len(str(value)) > 256:
        raise ValueError('A finite numeric tool cell is required; NULL is not zero')
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('Query normalized numeric cells before calculating') from exc
    if not number.is_finite() or abs(number.adjusted()) > 100 or len(number.as_tuple().digits) > 128:
        raise ValueError('Numeric cell is nonfinite or outside calculation limits')
    return number


def resolve_cell(results, reference):
    result = next((r for r in results if r.get('result_id') == reference.get('result_id')), None)
    row = reference.get('row')
    column = reference.get('column')
    if not result or result.get('error') or type(row) is not int or row < 0:
        raise ValueError('Unknown result reference or invalid zero-based row: '+str(reference)+
                         '; available result IDs: '+', '.join(str(r.get('result_id')) for r in results[-30:]))
    columns = result.get('columns') or []
    if columns.count(column) != 1:
        raise ValueError('Reference must name one unambiguous result column: '+str(result.get('result_id'))+
                         ':'+str(column)+'; available columns: '+', '.join(map(str, columns)))
    rows = result.get('rows') or []
    if row >= len(rows) or columns.index(column) >= len(rows[row]):
        raise ValueError('Referenced cell is outside the cached result: '+str(result.get('result_id'))+
                         ' has '+str(len(rows))+' rows; use zero-based row indexes')
    return rows[row][columns.index(column)], result


def calculate_comparison(results, action):
    operands = action.get('operands')
    if 'baseline' in action or 'current' in action:
        if operands is not None:
            raise ValueError('Use named baseline/current OR legacy operands, not both')
        operands = [action.get('baseline'), action.get('current')]
    if not isinstance(operands, list) or len(operands) != 2 or not all(isinstance(r, dict) for r in operands):
        raise ValueError('operands must be [baseline cell reference, current cell reference]; literal operands are forbidden')
    if (re.search(r'current|本期', str(operands[0].get('column')), re.I)
            and re.search(r'base|基期', str(operands[1].get('column')), re.I)):
        raise ValueError('Operand direction reversed: use named baseline and current references')
    values = []
    for ref in operands:
        value, result = resolve_cell(results, ref)
        if result.get('truncated'):
            raise ValueError('Use complete SQL aggregates, not truncated inputs, for comparisons')
        values.append(None if value is None else decimal_value(value))
    baseline, current = values
    if any(v is None for v in values):
        column = 'difference' if action['operation'] == 'difference' else 'growth_rate_pct'
        return {'operation': action['operation'], 'columns': [column], 'rows': [[None]],
                'row_count': 1, 'truncated': False, 'value_status': 'missing_input',
                'derivation': {'operands': operands, 'formula': 'current-baseline' if column == 'difference' else '(current-baseline)/baseline*100'},
                'warning': 'An input is NULL; the result is missing, not zero'}
    with localcontext() as ctx:
        ctx.prec = 128
        ctx.clear_flags()
        difference = current - baseline
        if ctx.flags[Inexact]:
            raise ValueError('Difference exceeds supported exact decimal precision')
        if action['operation'] == 'difference':
            value = difference
            column = 'difference'
        else:
            column = 'growth_rate_pct'
            value = None if baseline == 0 else difference / baseline * 100
    return {'operation': action['operation'], 'columns': [column],
            'rows': [[None if value is None else str(value)]], 'row_count': 1, 'truncated': False,
            'derivation': {'operands': operands, 'formula': 'current-baseline' if column == 'difference' else '(current-baseline)/baseline*100'},
            'warning': 'Growth rate is undefined because baseline is zero' if value is None else None}


def calculation_requirement(question):
    """Narrow routing contract, not a scan of numbers in model prose."""
    if re.search(r'只读取|原样|原文|不做.*计算|不要.*(?:重算|求平均|求和)', question):
        return None
    if re.search(r'加权|weighted', question, re.I):
        return 'weighted_average'
    if re.search(r'比值|几倍|\bratio\b', question, re.I):
        return 'ratio'
    if (re.search(r'精确|十进制|ROUND_HALF_UP|四舍五入|大额精度', question, re.I)
            and re.search(r'差额|差值|相减|减基期|current.*baseline', question, re.I)):
        return 'difference'
    return None


def calculate_ratio(results, action):
    operands = [action.get('numerator'), action.get('denominator')]
    if not all(isinstance(r, dict) for r in operands):
        raise ValueError('ratio requires named numerator and denominator cell references')
    values = []
    for ref in operands:
        value, result = resolve_cell(results, ref)
        if result.get('truncated'):
            raise ValueError('Ratio requires complete input evidence')
        values.append(None if value is None else decimal_value(value))
    numerator, denominator = values
    if any(v is None for v in values):
        return {'operation': 'ratio', 'columns': ['ratio', 'ratio_pct'], 'rows': [[None, None]],
                'row_count': 1, 'truncated': False, 'value_status': 'missing_input',
                'derivation': {'operands': operands, 'formula': 'numerator/denominator'},
                'warning': 'An input is NULL; the result is missing, not zero'}
    with localcontext() as ctx:
        ctx.prec = 128
        ratio = None if denominator == 0 else numerator / denominator
        percent = None if ratio is None else ratio * 100
    return {'operation': 'ratio', 'columns': ['ratio', 'ratio_pct'],
            'rows': [[None if ratio is None else str(ratio), None if percent is None else str(percent)]],
            'row_count': 1, 'truncated': False,
            'derivation': {'operands': operands, 'formula': 'numerator/denominator'},
            'warning': 'Ratio is undefined because denominator is zero' if ratio is None else None}


def match_result_keys(results, action):
    left_id, right_id = action.get('left_result_id'), action.get('right_result_id')
    if not left_id or not right_id or left_id == right_id:
        raise ValueError('match_keys requires two distinct cached result IDs')
    def keys(result_id, column):
        result = next((r for r in results if r.get('result_id') == result_id), None)
        if not result or result.get('error') or result.get('truncated'):
            raise ValueError('match_keys requires complete successful cached inputs')
        if (result.get('columns') or []).count(column) != 1:
            raise ValueError('match_keys requires one unambiguous key column per result')
        values = [resolve_cell(results, {'result_id': result_id, 'row': i, 'column': column})[0]
                  for i in range(len(result.get('rows') or []))]
        if any(v is not None and (isinstance(v, bool) or not isinstance(v, (str, int, float))) for v in values):
            raise ValueError('match_keys accepts scalar string/numeric keys only')
        return values
    left = keys(left_id, action.get('left_key'))
    right = keys(right_id, action.get('right_key'))
    # Membership, not a cartesian join: repeated right keys never multiply left rows.
    token = lambda v: (type(v).__name__, v)
    right_keys = {token(v) for v in right if v is not None and v != ''}
    missing = [v for v in left if v is None or v == '' or token(v) not in right_keys]
    distinct = list(dict.fromkeys(v for v in missing if v is not None and v != ''))
    return {'operation': 'match_keys',
            'columns': ['total_rows', 'matched_rows', 'unmatched_rows', 'unmatched_distinct_keys', 'null_left_keys'],
            'rows': [[len(left), len(left)-len(missing), len(missing), len(distinct), sum(v is None or v == '' for v in left)]],
            'row_count': 1, 'truncated': False, 'unmatched_keys': distinct,
            'derivation': {'formula': 'exact typed key membership; preserve every left row; NULL/empty never matches',
                           'left_result_id': left_id, 'right_result_id': right_id,
                           'left_key': action.get('left_key'), 'right_key': action.get('right_key')},
            'right_duplicate_keys': len([v for v in right if v is not None and v != ''])-len(right_keys)}


def publication_answer(text, results, question=''):
    """Publish tool-owned metadata without a second model rewriting its meaning."""
    if not re.search(r'备注|remark|原样', question, re.I):
        for result in results:
            for column in result.get('columns') or []:
                if '_' in str(column):
                    text = re.sub(r'[（(]单位\s*[:：]\s*'+re.escape(str(column))+r'[^）)]*[）)]',
                                  '（字段：'+str(column)+'）', text)
    growth = [r for r in results if r.get('operation') == 'growth_rate']
    denominators = []
    for result in growth:
        operands = (result.get('derivation') or {}).get('operands') or []
        if len(operands) != 2:
            continue
        try:
            baseline, _ = resolve_cell(results, operands[0])
            baseline = format(decimal_value(baseline), 'f')
            denominators.append(baseline)
        except ValueError:
            continue
    if len(set(denominators)) == 1 and re.search(r'增长|同比|环比|增幅', question):
        baseline = denominators[0]
        # Only explicit denominator explanations for an unambiguous growth contract.
        text = re.sub(r'分母(?:为|是)[^。；;\n）)]*', '分母为基期工具值 '+baseline, text)
        text += '\n\n计算口径：增长率 =（本期 − 基期）÷ 基期 × 100%；基期工具值为 '+baseline+'。'
    return text


def calculate_weighted_average(results, action):
    value_column, weight_column = action.get('value_column'), action.get('weight_column')
    if not value_column or not weight_column:
        raise ValueError('weighted_average requires value_column and weight_column from raw query rows')
    numerator = denominator = Decimal(0)
    count = 0
    with localcontext() as ctx:
        ctx.prec = 128
        ctx.clear_flags()
        for result in results:
            if result.get('truncated') or result.get('error'):
                raise ValueError('Weighted average requires complete query inputs')
            for index in range(len(result.get('rows') or [])):
                value, _ = resolve_cell([result], {'result_id': result['result_id'], 'row': index, 'column': value_column})
                weight, _ = resolve_cell([result], {'result_id': result['result_id'], 'row': index, 'column': weight_column})
                numerator += decimal_value(value) * decimal_value(weight)
                denominator += decimal_value(weight)
                count += 1
        if ctx.flags[Inexact]:
            raise ValueError('Weighted sum exceeds supported exact decimal precision')
        if not count:
            raise ValueError('No numeric rows available for weighted average')
        average = None if denominator == 0 else numerator / denominator
    return {'operation': 'weighted_average',
            'columns': ['weighted_total', 'weight_total', 'weighted_average'],
            'computed_columns': ['weighted_total', 'weight_total', 'weighted_average'],
            'rows': [[str(numerator), str(denominator), None if average is None else str(average)]],
            'row_count': 1, 'truncated': False,
            'derivation': {'value_column': value_column, 'weight_column': weight_column, 'formula': 'SUM(value*weight)/SUM(weight)'},
            'warning': 'Weighted average is undefined because total weight is zero' if average is None else None}


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {'style', 'script'}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {'style', 'script'}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def visible_text(text, is_html=False):
    if not is_html:
        return text
    parser = _VisibleText()
    parser.feed(text)
    return '\n'.join(parser.parts)


def numeric_answer_required(question, text, results, is_html=False):
    return bool(re.search(r'\{\{(?:result|aggregate)-', text, re.I))


def computed_kinds(result, column):
    if column in result.get('decimal_columns', []):
        return {'decimal_expression'}
    if result.get('operation') in {'sum', 'shares', 'compare_totals', 'difference', 'growth_rate', 'weighted_average', 'ratio'}:
        return {result['operation']} if column in result.get('computed_columns', result.get('columns', [])) else set()
    # Conservative direct-projection check. Complex CTE projections can instead
    # use aggregate_results over the pinned cells; never guess column lineage.
    try:
        import sqlglot
        from sqlglot import exp
        query = sqlglot.parse_one(result.get('sql') or '', read='sqlite')
        projection = query.selects[result['columns'].index(column)]
        if not query.find(exp.Table):
            return set()
        aggregate = projection.find(exp.AggFunc)
        division, subtraction = projection.find(exp.Div), projection.find(exp.Sub)
        if division and projection.find(exp.Column):
            return {'growth_rate' if subtraction else 'ratio'}
        if subtraction and projection.find(exp.Column):
            return {'difference'}
        if aggregate:
            if isinstance(aggregate, (exp.Sum, exp.Count)):
                return {'sum'}
            return {'average' if isinstance(aggregate, exp.Avg) else 'aggregate'}
        if any(projection.find(kind) for kind in (exp.Add, exp.Mul, exp.Mod)) and projection.find(exp.Column):
            return {'calculation'}
        return set()
    except (ValueError, IndexError, AttributeError, sqlglot.errors.ParseError):
        return set()


def scope_labels(catalogs, results, snapshots):
    """Scope labels come from executed table references, never business counts."""
    import sqlglot
    from sqlglot import exp
    labels = []
    for book in catalogs:
        workbook_id = book.get('workbook_id')
        authorized = {t.get('table_name') for t in (book.get('catalog') or {}).get('tables', [])}
        queried = set()
        for result in results:
            if (result.get('source') or {}).get('workbook_id') != workbook_id:
                continue
            try:
                query = sqlglot.parse_one(result.get('sql') or '', read='sqlite')
                queried.update(t.name for t in query.find_all(exp.Table) if t.name in authorized)
            except (sqlglot.errors.ParseError, AttributeError):
                continue
        if queried:
            labels.extend(str(len(queried)) + suffix for suffix in
                          ('个区域表', '区域表', '个区域', '张区域表', '个工作表', '张工作表', '个表', '张表'))
        version = (snapshots.get(workbook_id) or {}).get('version')
        if type(version) is int:
            labels.extend([f'v{version}', f'版本{version}', f'版本 {version}'])
    return labels


class ResultReferenceError(ValueError):
    """An explicitly requested cached cell cannot be safely resolved."""


def validate_scenario_reference(results, reference, question, visited=None):
    # Only explicit scenario columns and exact labels; no generic number scanner.
    if not question or re.search(r'原文|原样|只读取', question):
        return
    visited = set() if visited is None else visited
    key = (reference.get('result_id'), reference.get('row'), reference.get('column'))
    if key in visited:
        return
    visited.add(key)
    _, result = resolve_cell(results, reference)
    for index, column in enumerate(result.get('columns') or []):
        if not re.fullmatch(r'scenario|场景|场景名称', str(column), re.I):
            continue
        labels = {str(row[index]) for row in result.get('rows') or []
                  if index < len(row) and isinstance(row[index], str) and len(row[index]) >= 2}
        requested = {label for label in labels if label in question}
        if len(requested) == 1:
            row = result['rows'][reference['row']]
            if index >= len(row) or str(row[index]) not in requested:
                raise ResultReferenceError('Reference row does not match the explicitly requested scenario; filter by scenario first')
    for operand in (result.get('derivation') or {}).get('operands') or []:
        validate_scenario_reference(results, operand, question, visited)


def validate_catalog_claim(text, catalogs, question='', results=None):
    if re.search(r'原文|原样|只读取', question):
        return
    table_count = sum(len((c.get('catalog') or {}).get('tables') or []) for c in catalogs)
    if table_count > 1:
        for match in re.finditer(r'唯一(?:的)?工作表|只有(?:一个|1个|一张|1张)工作表', text):
            if not re.search(r'不是|并非|不能|不代表', text[max(0, match.start()-12):match.start()]):
                raise ResultReferenceError('The authorized catalog contains multiple tables; restrict absence claims to inspected tables')
        if results is not None and re.search(r'(?:本|整个|这份)工作簿(?:中)?(?:\s|\*|均|全部|完全)*(?:缺少|没有|不存在)', text):
            import sqlglot
            from sqlglot import exp
            covered = set()
            for result in results:
                try:
                    query = sqlglot.parse_one(result.get('sql') or '', read='sqlite')
                    covered.update(((result.get('source') or {}).get('workbook_id'), t.name)
                                   for t in query.find_all(exp.Table))
                except (sqlglot.errors.ParseError, AttributeError):
                    continue
            expected = {(c.get('workbook_id'), t.get('table_name')) for c in catalogs
                        for t in (c.get('catalog') or {}).get('tables') or []}
            if not expected.issubset(covered):
                raise ResultReferenceError('Workbook-wide absence is not established. State only the inspected table scope; other tables remain unverified')


def validate_report_comparisons(text, results, question=''):
    # Narrow publication contract for explicit numeric multipliers, not a number scan.
    if re.search(r'原文|原样|只读取', question):
        return
    if re.search(r'\d+(?:\.\d+)?\s*倍', visible_text(text, True)):
        if not any('ratio' in computed_kinds(r, c) for r in results for c in r.get('columns') or []):
            raise ResultReferenceError('Report multiplier has no ratio tool evidence. Omit unrequested multipliers or compute and cite ratio first')


def numeric_observation(question, text, results, is_html=False):
    # ponytail: heuristic audit only; this is not a semantic correctness check.
    if not DERIVED.search(visible_text(text, is_html)):
        return None
    computed = any(computed_kinds(r, c) for r in results for c in r.get('columns', []))
    return {'mode': 'observe', 'validation_scope': 'explicit_references_only',
            'risks': [] if computed else ['calculation_evidence_missing'],
            'evidence_count': len(results),
            'partial_evidence_present': any(r.get('truncated') for r in results)}


def render_numeric_answer(text, results, is_html=False, question='', labels=()):
    """Resolve optional references, without scanning or rejecting ordinary numbers."""
    references = []
    residual = REFERENCE.sub('', text)
    if re.search(r'\{\{(?:result|aggregate)-', residual, re.I):
        raise ResultReferenceError('Malformed result reference')

    def replacement(match):
        result_id, row, column, places = match.groups()
        ref = {'result_id': result_id, 'row': int(row), 'column': column}
        try:
            value, result = resolve_cell(results, ref)
            validate_scenario_reference(results, ref, question)
            kinds = computed_kinds(result, column)
            if value is None:
                rendered = ('输入缺失，无法计算' if result.get('value_status') == 'missing_input' else
                            '未定义（基数为零）' if result.get('operation') == 'growth_rate' else '空值（NULL）')
            elif isinstance(value, str) and places is None and not (kinds and re.fullmatch(r'[-+]?\d+\.\d{13,}', value)):
                # Identifiers and source text are not numbers: preserve leading zeros.
                rendered = value
            else:
                if isinstance(value, str) and not kinds:
                    raise ValueError('Raw text cells cannot be rounded')
                number = decimal_value(value)
                with localcontext() as ctx:
                    ctx.prec = 128
                    if places is None and kinds and number.as_tuple().exponent < -12:
                        number = number.quantize(Decimal('0.000000000001'), rounding=ROUND_HALF_UP)
                    if places is not None:
                        if int(places) > 12:
                            raise ValueError('Rounding must be between 0 and 12 decimal places')
                        number = number.quantize(Decimal(1).scaleb(-int(places)), rounding=ROUND_HALF_UP)
                    rendered = format(number, 'f')
        except (ValueError, InvalidOperation) as exc:
            raise ResultReferenceError(str(exc)) from exc
        references.append(dict(ref, rendered=rendered, source=result.get('source'),
                               input_result_ids=result.get('input_result_ids'),
                               metric_kinds=sorted(kinds), truncated=bool(result.get('truncated')),
                               validation_scope='explicit_references_only'))
        return html.escape(rendered) if is_html else rendered

    return REFERENCE.sub(replacement, text), references
