"""Small typed statistics operations; no question keywords or business defaults."""
from decimal import Decimal, Inexact, localcontext
from .numeric_evidence import decimal_value


def query_type_advisories(sql, catalog):
    """Advisory only: no SQL rewriting, data coercion, or publication rejection."""
    import sqlglot
    from sqlglot import exp
    from .analysis_result import physical_columns
    try:
        query = sqlglot.parse_one(sql, read='sqlite')
    except sqlglot.errors.ParseError:
        return []
    tables = {t.name.casefold() for t in query.find_all(exp.Table)}
    numeric = {c['physical_name'].casefold()
               for t in (catalog.get('catalog') or {}).get('tables', [])
               if t['table_name'].casefold() in tables for c in physical_columns(t)
               if str(c.get('sql_type', '')).upper() in {'INTEGER', 'INT', 'REAL', 'FLOAT', 'DOUBLE', 'NUMERIC', 'DECIMAL'}}
    columns = set()
    for cast in query.find_all(exp.Cast):
        predicate = cast.find_ancestor(exp.Predicate)
        if (not isinstance(cast.this, exp.Column) or cast.this.name.casefold() not in numeric
                or cast.args['to'].sql().upper() not in {'TEXT', 'VARCHAR', 'CHAR', 'NVARCHAR'}
                or predicate is None):
            continue
        for literal in predicate.find_all(exp.Literal):
            if literal.is_string:
                try:
                    decimal_value(literal.this)
                except (ValueError, ArithmeticError):
                    continue
                columns.add(cast.this.name)
    return [{'code': 'numeric_text_filter', 'columns': sorted(columns),
             'message': 'A numeric column is cast to TEXT for a numeric-looking filter. SQLite integer 1 and real 1.0 stringify differently. Numeric equality should use numeric operands on the typed column (or its authorized _value field). Check the predicate and coverage; retain exact text comparison for identifiers. SQL and results were not altered.'}] if columns else []


def profile_sql(table, columns, catalog):
    tables = {t['table_name']: t for t in (catalog.get('catalog') or {}).get('tables', [])}
    from .analysis_result import physical_columns
    if table not in tables:
        raise ValueError('Profile table must belong to the authorized catalog')
    known = {c['physical_name'] for c in physical_columns(tables[table])}
    if not isinstance(columns, list) or not 1 <= len(columns) <= 12 or any(not isinstance(c, str) or c not in known for c in columns):
        raise ValueError('Profile requires 1 to 12 authorized physical columns')
    quote = lambda s: '"'+s.replace('"', '""')+'"'
    queries = []
    for column in dict.fromkeys(columns):
        c = quote(column)
        queries.append("SELECT '"+column.replace("'", "''")+"' AS column_name, COUNT(*) AS total_rows, "
            +f"COUNT(CASE WHEN {c} IS NULL THEN 1 END) AS null_count, "
            +f"COUNT(CASE WHEN {c} IS NOT NULL AND TRIM(CAST({c} AS TEXT))='' THEN 1 END) AS blank_count, "
            +f"COUNT(CASE WHEN {c} IS NOT NULL AND TRIM(CAST({c} AS TEXT))<>'' THEN 1 END) AS nonempty_count "
            +f"FROM {quote(table)}")
    return ' UNION ALL '.join(queries)


def shares(results, action):
    """Every row's contribution to the sum of all selected, nonoverlapping rows."""
    column = action.get('value_column')
    if not results or not isinstance(column, str):
        raise ValueError('shares requires complete result_ids and value_column')
    columns = results[0].get('columns', [])
    if columns.count(column) != 1 or any(c in columns for c in ('share_total', 'share_pct')):
        raise ValueError('shares requires an unambiguous value_column and unused share_total/share_pct names')
    rows = []
    known_units = {r.get('column_units', {}).get(column) for r in results} - {None, ''}
    if len(known_units) > 1:
        raise ValueError('shares cannot combine incompatible known units')
    for result in results:
        if result.get('truncated') or result.get('columns') != columns:
            raise ValueError('shares requires complete inputs with the same columns')
        rows.extend(list(r) for r in result.get('rows', []))
    index = columns.index(column)
    values = [None if r[index] is None else decimal_value(r[index]) for r in rows]
    with localcontext() as ctx:
        ctx.prec = 128
        ctx.clear_flags()
        total = None if not values or None in values else sum(values, Decimal(0))
        if ctx.flags[Inexact]:
            raise ValueError('Share total exceeds supported exact decimal precision')
        output = [r+[None if total is None else str(total),
                      None if total in (None, 0) else str(v/total*100)] for r, v in zip(rows, values)]
    return {'operation': 'shares', 'columns': list(columns)+['share_total', 'share_pct'],
            'computed_columns': ['share_total', 'share_pct'], 'rows': output,
            'row_count': len(output), 'truncated': False,
            'warning': 'empty_input' if not rows else 'missing_input' if total is None else 'zero_denominator' if total == 0 else None,
            'derivation': {'value_column': column, 'scope': 'all selected rows, preserving duplicates',
                           'formula': 'value / SUM(value across ALL selected rows) * 100'}}


def compare_totals(results, action):
    baseline = action.get('baseline_column')
    current = action.get('current_column')
    if not isinstance(baseline, str) or not isinstance(current, str) or baseline == current:
        raise ValueError('compare_totals requires distinct baseline_column and current_column')
    values = {baseline: [], current: []}
    lineage = []
    for result in results:
        if result.get('truncated'):
            raise ValueError('compare_totals requires complete inputs')
        columns = result.get('columns') or []
        if any(columns.count(c) != 1 for c in values):
            raise ValueError('Every input must contain unambiguous baseline/current columns')
        for c in values:
            for row in result.get('rows') or []:
                v = row[columns.index(c)]
                values[c].append(None if v is None else decimal_value(v))
        lineage.append({'result_id': result['result_id'], 'baseline_column': baseline,
                        'current_column': current, 'scope': 'all cached rows'})
    with localcontext() as ctx:
        ctx.prec = 128
        ctx.clear_flags()
        totals = [None if not values[c] or None in values[c] else sum(values[c], Decimal(0)) for c in (baseline, current)]
        if ctx.flags[Inexact]:
            raise ValueError('Totals exceed supported exact decimal precision')
        a, b = totals
        diff = None if a is None or b is None else b-a
        if ctx.flags[Inexact]:
            raise ValueError('Difference exceeds supported exact decimal precision')
        rate = None if a is None or b is None or a == 0 else diff/a*100
    return {'operation': 'compare_totals',
            'columns': ['baseline_total', 'current_total', 'difference', 'growth_rate_pct'],
            'rows': [[None if v is None else str(v) for v in [a, b, diff, rate]]],
            'row_count': 1, 'truncated': False,
            'value_status': 'missing_input' if a is None or b is None else 'zero_baseline' if a == 0 else 'computed',
            'derivation': {'inputs': lineage, 'formula': 'sum(current)-sum(baseline); difference/sum(baseline)*100'}}
