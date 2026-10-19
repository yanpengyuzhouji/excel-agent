"""Replay known bad answers through semantic observation, without changing first-run scores."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import load_config
from app.service.llm import ExcelLLM
from app.agent.budget import AgentBudget
from app.agent.evidence_publication import review_publication


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('run', type=Path)
    p.add_argument('--case', action='append', required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    config = next(m for m in load_config()['excel']['llms'] if m['name'] == args.model)
    llm = ExcelLLM(config)
    llm.request_phase = 'publication_review'
    rows = []
    for case in args.case:
        result = json.loads((args.run/case/'result.json').read_text(encoding='utf-8'))
        events = json.loads((args.run/case/'events.json').read_text(encoding='utf-8'))
        evidence = [e for e in events if e.get('type') in {'source.query.result', 'aggregate.result'}]
        publication = (result.get('done') or {}).get('publication') or {'text': result['answer'], 'references': []}
        verdict = review_publication(llm, result['question'], publication, evidence, AgentBudget())
        row = {'id': case, **verdict, 'known_bad_rejected': verdict['decision'] == 'revise'}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    args.output.write_text(json.dumps({'model': args.model, 'rows': rows,
        'passed': all(r['known_bad_rejected'] for r in rows)}, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if all(r['known_bad_rejected'] for r in rows) else 1


if __name__ == '__main__':
    raise SystemExit(main())
