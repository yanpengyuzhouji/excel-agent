"""Targeted scenario checks; unknown answer layouts require manual review."""
from decimal import Decimal
import re


def scenario_check(case_id, text):
    if case_id not in {'A08', 'C06'}:
        return None
    scenario, expected = ('零基期', Decimal('50')) if case_id == 'A08' else ('四舍五入', Decimal('0.01'))
    header = None
    for line in text.splitlines():
        if '|' not in line:
            continue
        cells = [re.sub(r'[*`\s]', '', c) for c in line.strip().strip('|').split('|')]
        if any('场景' in c for c in cells):
            header = cells
            continue
        if not header or not cells or cells[0] != scenario:
            continue
        indexes = [i for i, c in enumerate(header) if
                   ('差额' in c if case_id == 'A08' else bool(re.search(r'ROUND_HALF_UP|舍入|2位|两位', c, re.I)))]
        if indexes:
            cell = cells[indexes[-1]] if indexes[-1] < len(cells) else ''
            match = re.search(r'[-+−]?\d+(?:\.\d+)?', cell)
            actual = Decimal(match.group().replace('−', '-')) if match else None
            return {'scenario': scenario, 'expected': str(expected), 'actual': str(actual), 'passed': actual == expected}
    # Prose fallback is deliberately narrow; never accept a number elsewhere.
    pattern = (r'差额(?:[（(][^）)\n]{0,24}[）)])?\s*[*`]*\s*(?:为|[:：=])\s*[*`]*\s*([-+−]?\d+(?:\.\d+)?)'
               if case_id == 'A08' else
               r'(?:ROUND_HALF_UP|四舍五入|舍入)[^。；;\n]{0,30}?(?:为|得到|[:：=])\s*[*`]*\s*([-+−]?\d+(?:\.\d+)?)')
    candidates = [Decimal(m.replace('−', '-')) for m in re.findall(pattern, text, re.I)]
    return {'scenario': scenario, 'expected': str(expected), 'candidates': [str(v) for v in candidates],
            'passed': bool(candidates) and all(v == expected for v in candidates),
            'manual_review_required': not bool(candidates)}
