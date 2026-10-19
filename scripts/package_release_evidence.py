"""Assemble immutable-image acceptance evidence, without rewriting first results."""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--release-dir', type=Path, required=True)
    parser.add_argument('--ops', type=Path, required=True)
    parser.add_argument('--run', type=Path, action='append', required=True)
    parser.add_argument('--review', type=Path, help='Separate semantic adjudications and inspected report images')
    args = parser.parse_args()
    folder = args.release_dir
    units = read(folder / 'unit.json')
    ops = read(args.ops / 'ops.json')
    image = subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Id}}', args.image], text=True).strip()
    archives = list(folder.glob('*linux-amd64.tar'))
    if len(archives) != 1:
        raise ValueError('Exactly one image archive required')
    archive = archives[0]
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    rounds = []
    review = read(args.review) if args.review else {}
    decisions = {(a['run'], a['id']): a for a in review.get('adjudications', [])}
    for path in args.run:
        raw = read(path / 'acceptance.json')
        diagnostic = read(path / 'model-diagnostics.json')
        audit = read(path / 'execution-audit.json')
        def reviewed_question(q):
            if q['status'] == 'auto_pass_pending_semantic_review':
                return True
            decision = decisions.get((path.name, q['id']), {})
            return bool(q['status'] == 'needs_review' and not q.get('html') and
                        decision.get('verdict') == 'screening_false_negative' and decision.get('reason') and
                        all(m['matched'] for m in q.get('text_checks', {}).get('metrics', [])) and
                        all(t['queried'] for t in q.get('evidence_tables', [])))
        rounds.append({
            'path': str(path.resolve()), 'started_at': raw['started_at'],
            'finished_at': raw.get('finished_at'), 'model': raw['model'],
            'image_before': raw['image_before'], 'image_after': raw.get('image_after'),
            'suite_sha256': raw['suite_sha256'], 'completed': raw.get('completed_turns'),
            'summary': dict(Counter(q['status'] for q in raw['questions'])),
            'screening_passed': raw.get('screening_passed', False),
            'provider_attempts': diagnostic['total_attempts'],
            'provider_outcomes': diagnostic['outcomes'],
            'internal_recovery_attempts': diagnostic['recovery_attempts'],
            'scope_failures': audit['scope_failures'],
            'reviewed_questions_passed': all(reviewed_question(q) for q in raw['questions']),
            'delivery_checks_passed': all(c['passed'] for c in raw['cleanup']) and
                raw.get('http', {}).get('passed') == len(raw['prepared_sources']) and
                all(n['passed'] for n in raw.get('negative_http', [])),
        })
    gates = {
        'unit_suite': units['passed'],
        'ops_complete': ops.get('passed', False),
        'ops_same_image': ops['image'] == image,
        'two_full_rounds': len({p.resolve() for p in args.run}) >= 2 and all(r['completed'] == 64 for r in rounds),
        'sequential_rounds': all(datetime.fromisoformat(a['finished_at']) <= datetime.fromisoformat(b['started_at'])
                                 for a,b in zip(rounds,rounds[1:]) if a['finished_at']),
        'same_image': all(r['image_before'] == r['image_after'] == image for r in rounds),
        'same_suite': len({r['suite_sha256'] for r in rounds}) == 1,
        'question_screening': all(r['screening_passed'] for r in rounds),
        'source_scope': all(not r['scope_failures'] for r in rounds),
    }
    source_files = sorted((ROOT / 'app').rglob('*.py')) + [ROOT / n for n in ('Dockerfile', 'config.yaml', 'requirements.lock', 'compose.yaml')]
    code = ("from pathlib import Path; import hashlib,json; root=Path('/app'); "
            "files=sorted((root/'app').rglob('*.py'))+[root/'config.yaml',root/'requirements.lock']; "
            "print(json.dumps({p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files}))")
    image_sources = json.loads(subprocess.check_output(
        ['docker','run','--rm','--entrypoint','python',image,'-c',code],text=True))
    source_hashes = {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files}
    gates['source_matches_image'] = all(source_hashes.get(k) == v for k,v in image_sources.items())
    result = {
        'image_tag': args.image, 'image_id': image, 'platform': 'linux/amd64',
        'archive': str(archive.resolve()), 'archive_sha256': digest,
        'unit': units, 'ops_path': str(args.ops.resolve()),
        'ops_checks': ops['checks'], 'rounds': rounds, 'gates': gates,
        'automated_service_acceptance': all(gates.values()),
        'review_record': str(args.review.resolve()) if args.review else None,
        'adjudications': review.get('adjudications', []),
        'source_sha256': source_hashes,
        'image_source_sha256': image_sources,
        'limits': ['Automated screening is not business-owner signoff.',
                   'PDF visual/semantic review is a separate retained record.',
                   '15-minute synthetic soak is not 24-hour production endurance.',
                   'Gateway authorization and multi-tenant isolation are outside this acceptance.'],
    }
    visuals = review.get('visual_reviews', [])
    expected_visuals = {(p.name, case) for p in args.run for case in ('R01', 'R02', 'T03')}
    visual_passed = expected_visuals == {(v['run'], v['id']) for v in visuals} and all(
        v.get('passed') and v.get('images') and all(Path(p).is_file() for p in v['images']) for v in visuals)
    result['visual_review_passed'] = visual_passed
    result['reviewed_service_acceptance'] = (all(v for k,v in gates.items() if k != 'question_screening') and
        all(r['reviewed_questions_passed'] and r['delivery_checks_passed'] for r in rounds) and visual_passed)
    (folder / 'acceptance-manifest.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'automated_service_acceptance': result['automated_service_acceptance'],
                     'reviewed_service_acceptance': result['reviewed_service_acceptance'],
                     'gates': gates, 'archive_sha256': digest}, ensure_ascii=False))


if __name__ == '__main__':
    main()
