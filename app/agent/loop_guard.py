"""Per-run duplicate-call protection; never shared across users or turns."""

import json


class ToolLoopGuard:
    def __init__(self):
        self.seen = set()
        self.repeated = 0

    def check(self, action, arguments, context=None):
        canonical = dict(arguments)
        if isinstance(canonical.get('sql'), str):
            # Preserve literals and SQL semantics; only trim boundary whitespace.
            canonical['sql'] = canonical['sql'].strip().rstrip(';').rstrip()
        key = (action, json.dumps(canonical, sort_keys=True, ensure_ascii=False), context)
        if key not in self.seen:
            self.seen.add(key)
            self.repeated = 0
            return None
        self.repeated += 1
        if self.repeated >= 3:
            return 'stop'
        return 'skip'


LOOP_REMINDER = (
    'Execution controller: this exact tool call was already attempted in this run. '
    'It has NOT been executed again. Observe its previous result or error above; '
    'reuse successful evidence, correct failed arguments, or choose a genuinely new action. '
    'If sufficient evidence exists, generate the requested report or terminate with the answer. '
    'Do not repeat identical calls or invent results.'
)

LOOP_ERROR = 'Tool loop stopped: repeated calls made no progress. Review existing evidence or change the request.'
