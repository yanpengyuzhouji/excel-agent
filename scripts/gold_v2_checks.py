"""Deterministic screening only; relational semantics still require review."""
from decimal import Decimal
from html.parser import HTMLParser
import re


def compact(text):
    return re.sub(r'\s+','',text).replace('\\*','*').replace('：',':')


def numbers(text):
    text=text.replace('−','-').replace('，',',')
    # Only conventional three-digit groups, never join across lines or arbitrary words.
    text=re.sub(r'(?<![\d.])[+-]?\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:\.\d+)?(?![\d.])',
                lambda m:re.sub(r'[ \u00a0\u202f]','',m.group()),text)
    values=[]
    for value in re.findall(r'(?<![A-Za-z0-9_.])[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![A-Za-z0-9_.])',text):
        try: values.append(Decimal(value.replace(',','')))
        except Exception: pass
    return values


def text_checks(case,text):
    candidates=numbers(text)
    metrics=[{'label':m['label'],'expected':m['value'],
              'matched':any(abs(n-Decimal(m['value']))<=Decimal(m['tolerance']) for n in candidates)} for m in case.get('metrics',[])]
    required=[{'text':v,'matched':compact(v) in compact(text)} for v in case.get('required_text',[])]
    # Case-specific equality wording; do not relax numeric or other case checks.
    if case.get('id') == 'C19':
        equality = bool(re.search(r'(?<!不)(?<!未)(?<!非)等于', text)) and not re.search(r'不等于|并非相等|不相等', text)
        for item in required:
            if item['text'] == '相等' and equality:
                item['matched'] = True
    pairs=[]
    for key,value in case.get('expected_pairs',[]):
        pairs.append({'key':key,'value':value,'matched':any(compact(key) in compact(line) and compact(value) in compact(line) for line in text.splitlines())})
    order=case.get('required_order',[])
    positions=[compact(text).find(compact(v)) for v in order]
    ordered=not order or all(p>=0 for p in positions) and positions==sorted(positions)
    refusal=case.get('kind')!='refusal' or any(v in text for v in case.get('refusal_terms',['缺少','没有','不包含','无法']))
    return {'metrics':metrics,'text':required,'pairs':pairs,'order_matched':ordered,
            'refusal_matched':refusal,'passed':all(v['matched'] for v in metrics+required+pairs) and ordered and refusal}


def packet_checks(case, packet):
    """Conservative labeled-metric screening, never a whole-answer number search.

    Unknown/paraphrased labels require review, not an inferred pass. Expected
    operations can additionally be specified by the independent golden oracle.
    This does not certify that a model-authored plan interprets the user correctly.
    """
    items = []
    for metric in case.get('metrics', []):
        labels = [metric['label'], *metric.get('label_aliases', [])]
        candidates = [t for t in packet.get('tasks', []) if t.get('status') == 'ok'
                      and t.get('operation') != 'table'
                      and any(compact(v).casefold() in compact(t.get('label', '')).casefold() for v in labels)
                      and (not metric.get('operation') or t.get('operation') == metric['operation'])]
        exact = [t for t in candidates if any(compact(t['label']).casefold() == compact(v).casefold() for v in labels)]
        suffix = [t for t in candidates if any(compact(t['label']).casefold().endswith(compact(v).casefold()) for v in labels)]
        candidates = exact or suffix or candidates
        matched = False
        for task in candidates if len(candidates) == 1 else []:
            if metric.get('unit') and compact(task.get('unit', '')) != compact(metric['unit']):
                continue
            try:
                matched |= abs(Decimal(str(task['value']))-Decimal(metric['value'])) <= Decimal(metric['tolerance'])
            except Exception:
                continue
        items.append({'label': metric['label'], 'expected': metric['value'], 'matched': matched,
                      'candidate_ids': [t['id'] for t in candidates]})
    return {'metrics': items, 'passed': all(m['matched'] for m in items),
            'scope': 'labeled_metrics_only_not_business_semantic_proof'}


class VisibleText(HTMLParser):
    def __init__(self):super().__init__();self.parts=[];self.hidden=0;self.has_table=False
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.hidden+=1
        if tag=='table':self.has_table=True
        if tag in ('p','tr','h1','h2','h3','div','li'):self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in ('script','style'):self.hidden=max(0,self.hidden-1)
    def handle_data(self,data):
        if not self.hidden:self.parts.append(data+' ')


def html_text(html):
    parser=VisibleText();parser.feed(html)
    return '\n'.join(parser.parts),parser.has_table
