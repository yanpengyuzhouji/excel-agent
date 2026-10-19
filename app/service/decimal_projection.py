"""Opt-in decimal output expressions; never rewrite SQL scope or ordering.

Only simple aliased projections are supported. Unsupported SQL stays native and
is not labeled decimal-exact. Source floats retain their stored precision.
"""
from decimal import Decimal, ROUND_HALF_UP, localcontext
import sqlglot
from sqlglot import exp


def compile_decimal_projection(sql):
    try:
        statements = sqlglot.parse(sql, read='sqlite')
        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            return sql, []
        query = statements[0]
        if any(query.args.get(k) for k in ('group', 'having', 'distinct', 'with_')):
            return sql, []
        if any(query.find(t) for t in (exp.AggFunc, exp.Window, exp.Subquery, exp.CTE)):
            return sql, []
        aliases = {p.alias.casefold() for p in query.selects if p.alias}
        order = query.args.get('order')
        # Only source-column ordering is independent of rewritten outputs.
        # Output aliases, ordinals and expressions retain native SQLite semantics.
        if order and any(not isinstance(item.this, exp.Column) or item.this.name.casefold() in aliases
                         for item in order.expressions):
            return sql, []
        predicates = [query.args.get('where')] + [j.args.get('on') for j in query.args.get('joins', [])]
        if any(p and any(c.name.casefold() in aliases for c in p.find_all(exp.Column)) for p in predicates):
            return sql, []
        def convert(node):
            if isinstance(node, (exp.Column, exp.Literal, exp.Null)):
                return node.copy(), False
            if isinstance(node, (exp.Paren, exp.Cast)):
                child, changed = convert(node.this)
                if changed and isinstance(node, exp.Cast) and node.args['to'].sql().upper() not in {'TEXT', 'VARCHAR', 'CHAR', 'NVARCHAR'}:
                    raise ValueError('A lossy final cast is not a decimal output')
                result = node.copy(); result.set('this', child)
                return result, changed
            if isinstance(node, exp.Neg):
                child, _ = convert(node.this)
                return exp.Anonymous(this='_excel_dec_sub', expressions=[exp.Literal.number(0), child]), True
            if isinstance(node, (exp.Add, exp.Sub, exp.Mul)):
                left, _ = convert(node.this)
                right, _ = convert(node.expression)
                name = {exp.Add: 'add', exp.Sub: 'sub', exp.Mul: 'mul'}[type(node)]
                return exp.Anonymous(this='_excel_dec_'+name, expressions=[left, right]), True
            if isinstance(node, exp.Round):
                digits = node.args.get('decimals') or exp.Literal.number(0)
                if not isinstance(digits, exp.Literal) or digits.is_string or not digits.this.isdigit() or not 0 <= int(digits.this) <= 12:
                    raise ValueError('Unsupported decimal output precision')
                child, _ = convert(node.this)
                return exp.Anonymous(this='_excel_dec_round', expressions=[child, digits.copy()]), True
            raise ValueError('Unsupported expression; retain native SQL')
        columns = []
        for projection in query.selects:
            if not isinstance(projection, exp.Alias) or sum(1 for _ in projection.walk()) > 64:
                continue
            try:
                replacement, changed = convert(projection.this)
            except ValueError:
                continue
            if changed:
                projection.set('this', replacement)
                columns.append(projection.alias)
        return (query.sql(dialect='sqlite'), columns) if columns else (sql, [])
    except (sqlglot.errors.ParseError, AttributeError, RecursionError):
        return sql, []


def register_decimal_functions(connection):
    def number(value):
        if len(str(value)) > 256:
            raise ValueError('Decimal operand exceeds supported precision')
        result = Decimal(str(value))
        if not result.is_finite() or abs(result.adjusted()) > 100 or len(result.as_tuple().digits) > 128:
            raise ValueError('Decimal operand is nonfinite or out of range')
        return result
    def calculate(operation, left, right):
        if left is None or right is None:
            return None
        with localcontext() as ctx:
            ctx.prec = 128
            a, b = number(left), number(right)
            result = {'add': lambda: a+b, 'sub': lambda: a-b, 'mul': lambda: a*b}[operation]()
            return format(number(result), 'f')
    def rounded(value, digits):
        if value is None:
            return None
        if type(digits) is not int or not 0 <= digits <= 12:
            raise ValueError('Unsupported decimal output precision')
        with localcontext() as ctx:
            ctx.prec = 128
            return format(number(value).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP), 'f')
    for operation in ('add', 'sub', 'mul'):
        connection.create_function('_excel_dec_'+operation, 2,
            lambda a, b, op=operation: calculate(op, a, b), deterministic=True)
    connection.create_function('_excel_dec_round', 2, rounded, deterministic=True)
