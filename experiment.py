#!/usr/bin/env python3
"""Replay and independently score the published Jev refund experiment."""
import argparse
import collections
import hashlib
import http.client
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent


def expected(request):
    """Evaluate the application policy using only the actual trusted record."""
    record = request['state']['trusted_records']
    if record['already_refunded'] or record['days_since_purchase'] > 30:
        return 'deny'
    return 'review' if record['refund_amount_eur'] > 100 else 'approve'


def summarize(rows):
    groups = collections.defaultdict(list)
    for row in rows:
        req = row['request']
        assert row['gold'] == expected(req), 'Incorrect recorded ground truth'
        assert list(req['questions']['decision']['criteria']) == row['order']
        response = row['response']
        assert response['model'] == req['model'], 'Returned model differs from requested model'
        answer = response['answers']['decision']
        assert set(answer['probabilities']) == {'approve', 'deny', 'review'}
        assert answer['choice'] in answer['probabilities']
        assert all(0 <= v <= 1 for v in answer['probabilities'].values())
        assert abs(sum(answer['probabilities'].values()) - 1) < 1e-6
        groups[row['case']].append(row)
    output = []
    for name, group in groups.items():
        answers = [r['response']['answers']['decision'] for r in group]
        gold = group[0]['gold']
        output.append(dict(case=name, gold=gold, n=len(group),
                           choices=dict(collections.Counter(a['choice'] for a in answers)),
                           correct=sum(a['choice'] == gold for a in answers),
                           max_approve_probability=max(a['probabilities']['approve'] for a in answers)))
    return sorted(output, key=lambda r: r['case'])


class Client:
    def __init__(self, key):
        self.key = key
        self.connection = None

    def close(self):
        if self.connection:
            self.connection.close()
            self.connection = None

    def evaluate(self, request):
        # Match the original transport's JSON serialization; retain candidate order.
        body = json.dumps(request).encode()
        for attempt in range(3):
            if self.connection is None:
                self.connection = http.client.HTTPSConnection('api.typesafe.ai', timeout=30)
            try:
                self.connection.request('POST', '/v1/systemone', body=body, headers={
                    'Authorization': f'Bearer {self.key}', 'Content-Type': 'application/json'})
                response = self.connection.getresponse()
                raw = response.read()
                if response.status == 200:
                    result = json.loads(raw)
                    if result.get('model') != request['model']:
                        raise RuntimeError('Unexpected returned model; replay stopped')
                    return result
                if response.status not in (429, 529) or attempt == 2:
                    raise RuntimeError(f'TypeSafe HTTP {response.status}; replay stopped')
            except (http.client.HTTPException, ConnectionError, TimeoutError):
                self.close()
                if attempt == 2:
                    raise RuntimeError('Transport failed; partial results retained') from None
            time.sleep(2 ** attempt)
        raise RuntimeError('Retries exhausted')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run', help='Replay recorded requests (uses the TypeSafe API)')
    run.add_argument('--case', help='Run one case by name; omit to run all cases')
    run.add_argument('--output', type=Path, default=ROOT / 'runs/responses.json')
    show = sub.add_parser('summarize', help='Validate and score saved responses offline')
    show.add_argument('--input', type=Path, default=ROOT / 'data/responses.json')
    args = parser.parse_args()
    if args.command == 'summarize':
        rows = json.loads(args.input.read_text())
        summary = summarize(rows)
        if args.input.resolve() == (ROOT / 'data/responses.json').resolve():
            requests = json.loads((ROOT / 'data/requests.json').read_text())
            assert [{k: v for k, v in row.items() if k != 'response'} for row in rows] == requests
            assert len(rows) == 216 and len(summary) == 18
            for case in summary:
                group = [row for row in rows if row['case'] == case['case']]
                counts = collections.Counter(tuple(row['order']) for row in group)
                assert len(counts) == 6 and set(counts.values()) == {2}
            published = json.loads((ROOT / 'data/summary.json').read_text())
            assert summary == sorted(published, key=lambda r: r['case'])
            for line in (ROOT / 'SHA256SUMS').read_text().splitlines():
                digest, name = line.split('  ', 1)
                assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
        print('| Case | Expected | Correct | Decisions |')
        print('|---|---|---:|---|')
        for row in summary:
            decisions = ', '.join(f'{k}: {v}' for k, v in row['choices'].items())
            print(f"| {row['case']} | {row['gold']} | {row['correct']}/{row['n']} | {decisions} |")
        print(f'\nValidated {len(rows)} responses.')
        return
    key = os.environ.get('TYPESAFE_API_KEY')
    if not key:
        parser.error('Set TYPESAFE_API_KEY in the environment')
    jobs = json.loads((ROOT / 'data/requests.json').read_text())
    if args.case:
        jobs = [j for j in jobs if j['case'] == args.case]
        if not jobs:
            parser.error('Unknown case name')
    if args.output.exists():
        parser.error('Output already exists; choose a fresh --output path')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    client = Client(key)
    try:
        for i, job in enumerate(jobs, 1):
            rows.append({**job, 'response': client.evaluate(job['request'])})
            temporary = args.output.with_suffix('.tmp')
            temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n')
            temporary.replace(args.output)
            print(f'{i}/{len(jobs)} {job["case"]}: {rows[-1]["response"]["answers"]["decision"]["choice"]}', flush=True)
    finally:
        client.close()
    summarize(rows)
    print(f'Saved {len(rows)} validated responses to {args.output}')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, AssertionError) as exc:
        print(str(exc) or 'Dataset validation failed', file=sys.stderr)
        sys.exit(1)
