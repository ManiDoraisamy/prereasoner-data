"""Manifest/correctness bridge: browser reports reuse the existing dataset golds."""
import json
from pathlib import Path
import sys

from tests.test_datasets import (
    DATASET_DIR, EXAMPLE_MANIFEST, EVAL_MANIFEST, EXPECTED, REWRITE_EXPECTATIONS,
    _manifest_names, _eval_cases, grade_answer,
)


def manifest():
    out = []
    for name in sorted(_manifest_names(EXAMPLE_MANIFEST) | _manifest_names(EVAL_MANIFEST)):
        directory = DATASET_DIR / name
        kind, want = EXPECTED[name]
        prompt = (directory / 'prompt.txt').read_text(encoding='utf-8').strip()
        cases = [dict(question=prompt, expected=want, fx=kind == 'world+fx', followup=False)]
        seen = {prompt}
        for question, expected in REWRITE_EXPECTATIONS.get(name, []):
            cases.append(dict(question=question, expected=expected, fx=False, followup=True))
            seen.add(question)
        for question, expected, fx, chat in _eval_cases(directory) or []:
            if question not in seen:
                cases.append(dict(question=question, expected=expected, fx=fx, followup=True, chat=chat))
                seen.add(question)
        out.append(dict(dataset=name, cases=cases, files=[
            str(f.resolve()) for f in sorted(directory.iterdir()) if f.suffix in ('.csv', '.xls', '.xlsx')
        ]))
    return out


def grade(item):
    reason = grade_answer(item['response'], item['expected'], fx=item.get('fx', False),
                          followup=item.get('followup', False))
    return dict(passed=reason is None, reason=reason)


def regrade(report):
    """Re-score recorded answers, retaining original verdicts; not a new browser run."""
    cases = {(dataset['dataset'], case['question']): case
             for dataset in manifest() for case in dataset['cases']}
    for record in report['records']:
        if 'response' not in record:
            continue
        test = cases[(record['dataset'], record['question'])]
        traces = [t for t in record['response'].get('traces', []) if t.get('engine')]
        engine = traces[-1]['engine'] if traces else {}
        response = {**engine, 'result': engine.get('answer') or engine.get('result'),
                    'clarify': engine.get('status') == 'clarify' or engine.get('clarify')}
        verdict = grade({**test, 'response': response})
        mode = {'py': 'python', 'sql': 'sql', 'both': 'verify'}[record['mode']]
        execution = engine.get('execution') or {}
        if test['expected'] is not None and (
            execution.get('actual') != mode or (mode == 'verify' and not execution.get('verified'))
        ):
            verdict = dict(passed=False, reason=(verdict['reason'] or '') + '; requested execution mode not verified')
        if record.get('ui', {}).get('failure') or record.get('errors'):
            verdict = dict(passed=False, reason=(verdict['reason'] or '') + '; browser error')
        record['original_grade'] = {k: record.get(k) for k in ('passed', 'reason', 'expected')}
        record.update(verdict, expected=test['expected'])
    report['regraded'] = 'Current shared comparator; original responses and browser run unchanged.'
    return report


if __name__ == '__main__':
    if sys.argv[1:] == ['export']:
        print(json.dumps(manifest()))
    elif sys.argv[1:] == ['grade']:
        print(json.dumps(grade(json.load(sys.stdin))))
    elif len(sys.argv) == 3 and sys.argv[1] == 'regrade':
        print(json.dumps(regrade(json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))), indent=2))
    else:
        raise SystemExit('use export, grade, or regrade <browser-report.json>')
