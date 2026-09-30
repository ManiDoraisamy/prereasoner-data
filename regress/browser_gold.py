"""Manifest/correctness bridge: browser reports reuse the existing dataset golds."""
import json
import re
from pathlib import Path
import sys

from tests.test_datasets import (
    DATASET_DIR, EXAMPLE_MANIFEST, EVAL_MANIFEST, EXPECTED, REWRITE_EXPECTATIONS,
    _manifest_names, _eval_cases, grade_answer,
    FX_TOLERANCE,
)


def manifest():
    demo_names = _manifest_names(EXAMPLE_MANIFEST)
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
        out.append(dict(dataset=name, load_demo=name in demo_names, cases=cases, files=[
            str(f.resolve()) for f in sorted(directory.iterdir()) if f.suffix in ('.csv', '.xls', '.xlsx')
        ]))
    return out


def grade(item):
    response = item['response']
    fx = item.get('fx', False)
    reason = grade_answer(response, item['expected'], fx=fx,
                          followup=item.get('followup', False))
    if reason is None and fx:
        reason = _grade_fx_presentation(response, item['expected'])
    return dict(passed=reason is None, reason=reason)


def _grade_fx_presentation(response, expected):
    """Ensure the user-facing FX sentence agrees with the verified engine result and target."""
    try:
        target = str((response.get('currency') or {})['target']).upper()
        expected = float(expected)
    except (KeyError, TypeError, ValueError):
        return 'FX answer lacks a scalar expectation or typed target currency'
    reply = str(response.get('assistant_reply') or '')
    if not reply.strip():
        return 'FX answer lacks a user-facing reply to verify'
    number = r'(?<![\w.])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
    currency_markers = {
        'GBP': r'(?:£|\bGBP\b|\bBritish pounds?\b|\bpounds? sterling\b)',
        'USD': r'(?:\$|\bUSD\b|\bUS dollars?\b|\bUnited States dollars?\b)',
        'EUR': r'(?:€|\bEUR\b|\beuros?\b)',
    }.get(target, rf'\b{re.escape(target)}\b')
    # Bind the target unit to the *same* numeric token. Independent searches let an answer such
    # as "the conversion is £1,925; the actual total is £810" pass by finding 1,925 and GBP
    # separately. A short textual separator supports natural variants like "GBP total: 1,925".
    amount = re.compile(
        rf'(?:{currency_markers}\s*(?:(?:(?:total|amount)\s*[:=]?|is|of|:|=)\s*)?{number}'
        rf'|{number}\s*{currency_markers})', re.I,
    )
    if not re.search(currency_markers, reply, re.I):
        return f'user-facing FX answer does not identify target currency {target}'
    paired_values = []
    for match in amount.finditer(reply):
        numeric = re.search(number, match.group(0))
        if numeric:
            try:
                paired_values.append(float(numeric.group(0).replace(',', '')))
            except ValueError:
                pass
    # Fail closed if the reply explicitly pairs a target-currency label with a conflicting
    # amount, even if it also repeats the expected amount elsewhere.
    if not paired_values:
        return f'user-facing FX answer does not pair a value with target currency {target}'
    tolerance = abs(expected) * FX_TOLERANCE
    if not any(abs(value - expected) <= tolerance for value in paired_values):
        return f'user-facing FX answer omits the expected converted value {expected:g} {target}'
    if any(abs(value - expected) > tolerance for value in paired_values):
        return f'user-facing FX answer also states a conflicting {target} amount'
    return None


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
