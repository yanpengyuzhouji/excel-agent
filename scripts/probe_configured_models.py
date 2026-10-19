"""Non-business access/structured-control probes. Never print credentials or raw errors."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import load_config
from app.service.llm import ExcelLLM, LLMRequestError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('selectors', nargs='+')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    configs = {m['name']: m for m in load_config()['excel']['llms']}
    rows = []
    for selector in args.selectors:
        config = dict(configs[selector], max_tokens=1024)
        row = {'selector': selector, 'requested_model': config['model']}
        try:
            llm = ExcelLLM(config)
            llm.request_phase = 'agent_control'
            text = llm.generate_chat([{'role': 'system', 'content': 'Return a JSON object only.'},
                {'role': 'user', 'content': 'Return exactly {"action":"terminate","action_input":{"result":"OK"}}'}])
            body = json.loads(text)
            row.update(status='passed' if body.get('action') == 'terminate' and
                       (body.get('action_input') or {}).get('result') == 'OK' else 'format_failed',
                       finish_reason=llm.last_finish_reason)
        except LLMRequestError as exc:
            row.update(status='blocked', error_code=exc.code, request_id=exc.request_id)
        except Exception as exc:
            row.update(status='probe_error', exception_type=type(exc).__name__)
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'at_utc': datetime.now(timezone.utc).isoformat(),
                                     'non_business_probe': True, 'models': rows}, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if all(r['status'] == 'passed' for r in rows) else 1


if __name__ == '__main__':
    raise SystemExit(main())
