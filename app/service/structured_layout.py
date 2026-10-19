"""Conservative, coordinate-preserving layouts beyond one table per worksheet."""
import math
import re
from decimal import Decimal


def metric_number(raw):
    """Return a queryable value/unit without changing the original source text."""
    text = str(raw).strip() if raw is not None else ''
    match = re.fullmatch(r'([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(%|GB|MB|KB|TB|B|万元|元|项|个)?', text, re.I)
    if not match or re.match(r'[+-]?0\d', match[1]):
        return None, None
    value = Decimal(match[1].replace(',', ''))
    unit = match[2] or None
    if unit == '%':
        value /= 100
    number = float(value)
    if not math.isfinite(number) or Decimal(str(number)) != value or (value == value.to_integral_value() and Decimal.from_float(number) != value):
        return None, unit
    return number, unit


def metric_derived(raw, kind):
    value, unit = metric_number(raw)
    if kind == 'metric_numeric':
        return value
    if kind == 'metric_unit':
        return unit
    raise ValueError('Unknown derived metric transformation')


def _title(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r'\s*(={3,}|-{3,})\s*(.+?)\s*\1\s*', value)
    return match[2].strip() if match and len(match[2]) <= 150 else None


def parse_structured_sheet(parsed, sheet_id, table_base):
    # Lazy import keeps the shared primitive definitions in the existing parser.
    from .parser import (_is_empty, _column_letter, _parse_cell_reference,
                         _infer_column, _identifier_label, _identifier_value,
                         normalize_label, safe_identifier, HEADER_FIELD_WORDS, _excluded_row_reason)
    name, original_rows, merged, hidden_rows, hidden_columns, formulas = parsed[:6]
    facts = parsed[6] if len(parsed) > 6 else {}
    if not original_rows:
        return None
    rows = [list(row) for row in original_rows]
    headings = {}
    for number, row in enumerate(rows, 1):
        occupied = [(i, facts.get(f'{_column_letter(i)}{number}')) for i in range(1, len(row)+1)]
        occupied = [(i, f) for i, f in occupied if f and f.get('raw_value') not in (None, '')]
        if len(occupied) == 1 and occupied[0][0] == 1:
            fact = occupied[0][1]
            title = _title(fact.get('raw_value'))
            if title:
                row[0] = fact['raw_value']
                headings[number] = title

    def make_region(start, end, title, header_rows, metrics=False, column_bounds=None):
        first_col,last_col=column_bounds or (1,None)
        suffix=f'_col_{first_col}' if column_bounds else ''
        region_id = f'{sheet_id}_region_{start}{suffix}'
        region_facts = {c:f for c,f in facts.items() if start <= _parse_cell_reference(c)[0] <= end
                        and _parse_cell_reference(c)[1]>=first_col
                        and (last_col is None or _parse_cell_reference(c)[1]<=last_col)}
        classifications = {str(_parse_cell_reference(c)[0]):'title' for c in region_facts}
        fact_rows = {_parse_cell_reference(c)[0] for c in region_facts}
        active = sorted(fact_rows - headings.keys())
        header_end = max(header_rows) if header_rows else start-1
        data_indices = [r for r in active if r > header_end]
        width = last_col or max((len(rows[r-1]) for r in active), default=0)
        while width and all(_is_empty(rows[r-1][width-1]) and f'{_column_letter(width)}{r}' not in region_facts for r in active):
            width -= 1
        if metrics:
            width = 2
            labels, paths = ['指标', '原始值'], [[], []]
        else:
            labels, paths = [], []
            for r in header_rows:
                for col in range(first_col,width+1):
                    fact = facts.get(f'{_column_letter(col)}{r}')
                    if fact and (not isinstance(fact.get('value'),str) or
                                 fact.get('data_type') in {'n','d','f','b','e'} or fact.get('decoded_type') == 'd'):
                        return None  # Merged extent alone cannot turn a business record into a header.
            for col in range(first_col, width+1):
                parts, cells = [], []
                for r in header_rows:
                    coordinate = f'{_column_letter(col)}{r}'
                    item = merged.get(coordinate)
                    anchor = item['source_cell'] if item else coordinate
                    value = facts.get(anchor, {}).get('value')
                    if _is_empty(value) or not isinstance(value, str):
                        continue
                    part = str(value).strip()
                    if not parts or part != parts[-1]:
                        parts.append(part); cells.append(anchor)
                if not parts:
                    if not column_bounds: return None
                    labels.append(f'未命名列 {_column_letter(col)}'); paths.append([])
                    continue
                labels.append('_'.join(parts)); paths.append(cells)
            if len(set(normalize_label(v) for v in labels)) != len(labels):
                return None
        source_columns=list(range(first_col,width+1))
        width=len(source_columns)
        columns, used = [], set()
        column_formats = {}
        for coordinate, fact in region_facts.items():
            column_formats.setdefault(re.match(r'[A-Z]+',coordinate)[0],set()).add(fact.get('number_format','General'))
        for i, label in enumerate(labels, 1):
            source_col=source_columns[i-1]
            values = [rows[r-1][source_col-1] if source_col <= len(rows[r-1]) else None for r in data_indices]
            inference = _infer_column(values)
            if metrics or _identifier_label(label):
                inference.update(sql_type='TEXT', mixed_numeric=False)
            columns.append(dict(column_id=f'{region_id}_column_{i}', source_column=_column_letter(source_col),
                source_name=label, display_name=label,
                physical_name=('metric' if i == 1 else 'value_raw') if metrics else safe_identifier(label, f'column_{i}', used),
                enabled=True, nullable=True, empty_header=not metrics and not paths[i-1], header_cells=paths[i-1],
                header_path=label.split('_'), unit_hint=label,
                semantic_type=('identifier' if _identifier_label(label) else 'amount' if re.search(r'金额|投资|费用|预算|单价|总价',label) else 'percentage' if re.search(r'率|百分比',label) else 'value'),
                number_formats=sorted(column_formats.get(_column_letter(source_col),set())),
                **inference))
        if metrics:
            for physical, kind, sql_type in [('numeric_value','metric_numeric','REAL'), ('unit','metric_unit','TEXT')]:
                columns.append(dict(column_id=f'{region_id}_{physical}', source_column='B',
                    source_name=physical, display_name='规范数值' if sql_type == 'REAL' else '单位',
                    physical_name=physical, sql_type=sql_type, enabled=True, nullable=True,
                    empty_header=False, derived=kind, unit_hint='百分比为比值；其他单位不换算' if sql_type == 'REAL' else '原始单位'))
        data, excluded = [], []
        field_words = set(HEADER_FIELD_WORDS) | {'日期','数量','项目','金额','收入','地区','年份','类型','后缀'}
        for r in data_indices:
            raw = [rows[r-1][c-1] if c<=len(rows[r-1]) else None for c in source_columns]
            effective = list(raw)
            for i in range(width):
                coordinate = f'{_column_letter(source_columns[i])}{r}'
                item = merged.get(coordinate)
                if _is_empty(effective[i]) and item:
                    effective[i] = item['value']
                if _identifier_label(labels[i]):
                    effective[i] = _identifier_value(effective[i], facts.get(item['source_cell'] if item else coordinate))
            reason = None
            if not metrics:
                reason = _excluded_row_reason(effective,[normalize_label(v) for v in labels])
                if (reason != 'repeated_header' and r > header_end+1
                        and not any(not _is_empty(v) for v in rows[r-2])
                        and sum(normalize_label(v) in field_words for v in effective) >= 2):
                    return None  # Do not hide an untitled second schema inside a verified region.
            classifications[str(r)] = reason or 'data'
            if reason:
                excluded.append({'source_row':r,'reason':reason,'values':raw})
                continue
            if metrics:
                effective.extend(metric_number(raw[1]))
            data.append(dict(source_row=r, values=effective, raw_values=raw,
                             hidden=r in hidden_rows, cells={_column_letter(i):facts[f'{_column_letter(i)}{r}'] for i in source_columns if f'{_column_letter(i)}{r}' in region_facts}))
        if not data:
            return None
        if not metrics:
            for i,column in enumerate(columns):
                column.update(_infer_column(r['values'][i] for r in data))
                if _identifier_label(labels[i]):
                    column.update(sql_type='TEXT',mixed_numeric=False)
        local_merges = {c:m for c,m in merged.items() if start <= _parse_cell_reference(c)[0] <= end
                        and _parse_cell_reference(c)[1] in source_columns}
        for item in local_merges.values():
            begin, finish = item['range'].split(':')
            r1, c1 = _parse_cell_reference(begin); r2, c2 = _parse_cell_reference(finish)
            if r1 < start or r2 > end or c1<first_col or c2>source_columns[-1] or (r1 <= header_end < r2):
                return None
        return dict(sheet_id=region_id, name=name, region_title=title, region_range=f'{_column_letter(first_col)}{start}:{_column_letter(source_columns[-1])}{end}',
            layout_kind='metrics' if metrics else 'table', kind='data', enabled=True,
            table_name=f'{table_base}_region_{start}{suffix}', header_row=min(header_rows) if header_rows else None,
            header_rows=header_rows, data_start_row=min(data_indices), row_count=len(data), column_count=len(columns),
            source_record_count=len(data)+len(excluded), source_classifications=classifications,
            cells=region_facts, rows=data, columns=columns, excluded_rows=excluded, issues=[],
            **({'source_columns':[_column_letter(c) for c in source_columns]} if column_bounds else {}),
            hidden_rows=[r for r in hidden_rows if start <= r <= end], hidden_columns=hidden_columns,
            merged_ranges=sorted({m['range'] for m in local_merges.values()}), merge_lineage=local_merges,
            literal_headings={c:f['raw_value'] for c,f in region_facts.items() if _parse_cell_reference(c)[0] in headings},
            formulas={c:f for c,f in formulas.items() if c in region_facts and _parse_cell_reference(c)[0] not in headings}, metadata=[])

    def horizontal_regions():
        from .parser import _header_index, _formula_bridge, ENGINEERING_FIELDS
        occupied=sorted({_parse_cell_reference(c)[1] for c,f in facts.items() if f.get('raw_value') not in (None,'')})
        groups=[]
        for col in occupied:
            if not groups or col>groups[-1][-1]+1: groups.append([])
            groups[-1].append(col)
        if len(groups)<2 or any(len(g)<2 for g in groups): return None
        header=_header_index(rows)+1
        header_cells=list(rows[header-1])+[None]*max(0,max(occupied)-len(rows[header-1]))
        if any(_parse_cell_reference(c)[0]<header and f.get('raw_value') not in (None,'') for c,f in facts.items()): return None
        labels=[str(header_cells[c-1]).strip() for g in groups for c in g if not _is_empty(header_cells[c-1])]
        if len(set(labels))!=len(labels) or sum(v in ENGINEERING_FIELDS or v in HEADER_FIELD_WORDS for v in labels)<3:
            return None
        if any(_formula_bridge(facts,header,a[-1],b[0]) for a,b in zip(groups,groups[1:])):
            return None  # A locally referenced derived block belongs to the main table.
        result=[]
        for group in groups:
            header_values=[header_cells[c-1] for c in group if not _is_empty(header_cells[c-1])]
            if len(header_values)<2 or not all(isinstance(v,str) for v in header_values): return None
            region=make_region(header,len(rows),f'并排表区 {_column_letter(group[0])}:{_column_letter(group[-1])}',
                               [header],column_bounds=(group[0],group[-1]))
            if region is None: return None
            result.append(region)
        if set().union(*(set(r['cells']) for r in result))!=set(facts): return None
        return result

    regions = []
    side_regions=horizontal_regions() if not headings else None
    if headings:
        starts = sorted(headings)
        if any(f.get('raw_value') not in (None,'') and _parse_cell_reference(c)[0] < starts[0] for c,f in facts.items()):
            return None
        for index, start in enumerate(starts):
            end = starts[index+1]-1 if index+1 < len(starts) else len(rows)
            occupied = [r for r in range(start+1,end+1) if any(f'{_column_letter(i)}{r}' in facts for i in range(1,len(rows[r-1])+1))]
            if not occupied:
                return None
            first = rows[occupied[0]-1]
            filled = [v for v in first if not _is_empty(v)]
            fields = set(HEADER_FIELD_WORDS) | {'类型','后缀','数量','严重程度','损坏类型','金额'}
            tabular = len(filled)>=2 and all(isinstance(v,str) for v in filled) and any(str(v).strip() in fields for v in filled[1:])
            metrics = not tabular and all(len(rows[r-1])>=2 and isinstance(rows[r-1][0],str)
                and f'B{r}' in facts and not any(not _is_empty(v) for v in rows[r-1][2:]) for r in occupied)
            if not tabular and not metrics:
                return None
            region = make_region(start,end,headings[start],[occupied[0]] if tabular else [],metrics)
            if region is None:
                return None
            regions.append(region)
    elif side_regions:
        regions.extend(side_regions)
    elif merged:
        active = sorted({_parse_cell_reference(c)[0] for c,f in facts.items() if f.get('raw_value') not in (None,'')})
        if not active:
            return None
        first = active[0]
        width = max((_parse_cell_reference(c)[1] for c in facts),default=0)
        merge_bounds = []
        for item in {m['range']:m for m in merged.values()}.values():
            begin, finish = item['range'].split(':')
            r1,c1 = _parse_cell_reference(begin); r2,c2 = _parse_cell_reference(finish)
            merge_bounds.append((r1,c1,r2,c2))
        if (first,1,first,width) in merge_bounds:
            first = next((r for r in active if r > first),first)
        header_merges = [(r1,r2) for r1,c1,r2,c2 in merge_bounds if r1 == first]
        if not header_merges:
            return None
        end_header = max(max(r1+1,r2) for r1,r2 in header_merges)
        # Follow nested header merges, not a fixed two-row assumption.
        for _ in range(6):
            expanded = max([end_header] + [max(r2,r1+1 if c2>c1 else r2)
                for r1,c1,r2,c2 in merge_bounds if first <= r1 <= end_header])
            if expanded == end_header:
                break
            end_header = expanded
        if end_header-first > 5:
            return None
        horizontal = any(first <= r1 <= end_header and c2 > c1 for r1,c1,r2,c2 in merge_bounds)
        # Uniform vertical merges covering every column prove an empty header tier.
        # A single vertical label beside actual row-two values remains ambiguous.
        vertical_columns = {c1 for r1,c1,r2,c2 in merge_bounds
                            if r1 == first and r2 == end_header and c1 == c2}
        uniform_vertical = (vertical_columns == set(range(1,width+1)) and
            not any(first < _parse_cell_reference(c)[0] <= end_header
                    and f.get('raw_value') not in (None,'') for c,f in facts.items()))
        if not horizontal and not uniform_vertical:
            return None
        region = make_region(active[0],len(rows),'多层表头',list(range(first,end_header+1)))
        if region is None:
            return None
        regions.append(region)
    else:
        return None
    _reconcile_metrics(regions)
    return regions


def _reconcile_metrics(regions):
    """Source disagreements are evidence notes, never a reason to block import."""
    metrics = [(s, r) for s in regions if s['layout_kind'] == 'metrics' for r in s['rows']]
    for table in regions:
        labels = [c['source_name'] for c in table['columns']]
        if '数量' not in labels or '严重程度' not in labels:
            continue
        count_index, group_index = labels.index('数量'), labels.index('严重程度')
        grouped = {}
        for row in table['rows']:
            value, _ = metric_number(row['values'][count_index])
            if value is None:
                continue
            group = str(row['values'][group_index])
            grouped[group] = grouped.get(group, 0) + value
        for metric_table, row in metrics:
            key, raw, value = row['values'][:3]
            if re.search(r'[/、或及和]', key):
                continue  # A combined population is not one severity category.
            for group, total in grouped.items():
                if group and group in key and value is not None and value != total:
                    note = {'code':'SOURCE_SUMMARY_MISMATCH','level':'info',
                        'message':f'源表 {metric_table["name"]} B{row["source_row"]} 指标“{key}”为 {raw}，'
                                  f'{table["region_title"]} 按“{group}”合计为 {total:g}；两种来源均保留，回答应说明口径，不能自动改写。'}
                    metric_table.setdefault('quality_notes',[]).append(note)
                    table.setdefault('quality_notes',[]).append(note)


def validate_region_coverage(draft):
    """Every original occupied cell belongs to exactly one logical region."""
    if not draft.get('source_layouts'):
        return
    by_id = {s.get('sheet_id'):s for s in draft.get('sheets',[])}
    for source in draft.get('source_layouts',[]):
        found = set()
        for region_id in source['region_ids']:
            if region_id not in by_id:
                raise ValueError('Source region coverage is incomplete')
            region = by_id[region_id]
            if region['name'] != source['sheet_name']:
                raise ValueError('Source region coverage sheet mismatch')
            coordinates = set(region.get('cells',{}))
            if found & coordinates:
                raise ValueError('Source region coverage overlaps')
            found.update(coordinates)
        if found != set(source['cells']):
            raise ValueError('Source region coverage does not reconcile')
