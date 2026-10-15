"""Automatic import policy; materialization and SQL semantics are unchanged."""


def review_issues(import_response):
    validation = import_response.validation or {}
    errors = list(validation.get('errors', []))
    severe = [w for w in validation.get('warnings', [])
              if w.get('code') not in {'MIXED_NUMERIC_PLACEHOLDER', 'HIDDEN_ROWS', 'MERGED_CELLS', 'FORMULAS'}]
    # A formula without a cached value becomes NULL: never silently auto-commit.
    for sheet in import_response.sheets:
        if not sheet.get('enabled') or sheet.get('kind') != 'data':
            continue
        missing = sum(1 for formula in (sheet.get('formulas') or {}).values()
                      if formula.get('cached_value') is None)
        if missing and not any(w.get('code') == 'FORMULA_CACHE_MISSING' for w in severe):
            severe.append({'code': 'FORMULA_CACHE_MISSING', 'message':
                           f"{sheet.get('sheet_name') or sheet.get('name') or sheet.get('sheet_id')} 有 {missing} 个公式缺少缓存值，请在 Excel 中重算并保存，或明确接受空值。"})
    return errors, severe
