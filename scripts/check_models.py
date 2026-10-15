"""Read-only model discovery. Prints no credentials or provider response bodies."""
import argparse
import json

from app.config import build_app_config
from app.service.llm import ExcelLLM

parser = argparse.ArgumentParser()
parser.add_argument('--names', nargs='*')
parser.add_argument('--probe', action='store_true', help='Two bounded inference calls per model to check SQL and report protocol')
args = parser.parse_args()
cfg = build_app_config()
seen = set()
for model in [cfg.llm] + cfg.llms:
    name = model.get('name') or model.get('model')
    if name in seen:
        continue
    seen.add(name)
    if args.names and name not in args.names:
        continue
    try:
        client = ExcelLLM(dict(model, timeout=10, max_retries=0))
        available = [m.id for m in client._client.models.list().data]
        print(json.dumps({'name': name, 'connected': True, 'model_available': client.model in available}))
        if args.probe:
            client = ExcelLLM(dict(model, timeout=60, max_retries=0))
            from app.agent.react_loop import parse_react
            from app.agent.multi_workbook import MULTI_WORKBOOK_PROMPT
            sql_reply = client.generate_chat([{'role': 'system', 'content': MULTI_WORKBOOK_PROMPT}, {'role': 'user', 'content': 'Authorized workbook a, table data, physical column amount REAL. Query total amount using query_workbook now. Output Action and Action Input JSON.'}], max_tokens=1536)
            action, arguments = parse_react(sql_reply)
            sql_ok = action == 'query_workbook' and 'SELECT' in str(arguments.get('sql', '')).upper()
            html_reply = client.generate_chat([{'role': 'system', 'content': MULTI_WORKBOOK_PROMPT}, {'role': 'user', 'content': 'Execution controller: report_requested=true. Successful query evidence: total amount=10. Generate a report now. HTML must be under 500 characters, include html/head/body and total 10, no scripts. Use Action: html_interpreter and Action Input JSON with title and html.'}], max_tokens=8192)
            action, arguments = parse_react(html_reply)
            print(json.dumps({'name': name, 'sql_protocol_ok': sql_ok, 'html_protocol_ok': action == 'html_interpreter' and '<html' in str(arguments.get('html', '')).lower(), 'html_finish_reason': client.last_finish_reason}))
    except Exception as exc:
        print(json.dumps({'name': name, 'connected': False, 'error_type': type(exc).__name__}))
