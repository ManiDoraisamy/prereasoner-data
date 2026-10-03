"""Serving-scale gate: realistic synthetic subscriptions and independent Decimal gold."""
from __future__ import annotations

import csv
from decimal import Decimal
import json
import os
from pathlib import Path
import time


def fixture(count=30000):
    columns = ['id', 'customer_id', 'customer_email', 'domain', 'plan', 'product', 'Currency',
               'cancellation_reason', 'Amount', 'subscription', 'created', 'status']
    rows = [[f'sub_{i:08}', f'customer_{i%15000:08}', f'user{i}@example.invalid', 'example.invalid',
             'plan_yearly', 'Checklist', 'USD', 'requested' if i%2 else '',
             str(Decimal(i%100)/100), 'yearly', f'2026-09-{i%28+1:02}', 'canceled' if i%2 else 'active']
            for i in range(count)]
    return {'name':'subscriptions', 'columns':columns, 'rows':rows}


def write_fixture(path):
    table = fixture()
    with Path(path).open('w', encoding='utf-8', newline='') as stream:
        writer=csv.writer(stream);writer.writerow(table['columns']);writer.writerows(table['rows'])


def main():
    if not os.environ.get('KB_PG_PASSWORD'):
        raise RuntimeError('Scale gate requires the isolated seeded PostgreSQL runtime')
    from engine.knowledge import KnowledgeReasoner
    from regress.live_schema import live_schema, served
    table=fixture()
    gold=sum(Decimal(row[8]) for row in table['rows'])
    canceled=sum(Decimal(row[8]) for row in table['rows'] if row[-1]=='canceled')
    Q=KnowledgeReasoner();lease=live_schema();records=[]
    try:
        for mode in ('sql', None):
            for question, expected in [('How many subscriptions?', Decimal(30000)),
                                       ('total Amount', gold), ('total Amount for canceled', canceled)]:
                start=time.perf_counter()
                result=served(lease.name,Q.serve,[table],question,lease.name,mode=mode)
                elapsed=time.perf_counter()-start
                answer=(result.get('result') or {}).get('rows') or []
                assert not result.get('error') and not result.get('clarify'), result
                assert len(answer)==1 and len(answer[0])==1 and Decimal(str(answer[0][0]))==expected, (question,answer,expected)
                assert elapsed<90, (question,elapsed)
                records.append({'rows':30000,'columns':12,'question':question,'mode':mode or 'auto',
                                'seconds':round(elapsed,3),'gold':str(expected),'execution':result.get('execution')})
                print('scale',json.dumps(records[-1]),flush=True)
        start=time.perf_counter()
        result=served(lease.name,Q.serve,[table],'show all rows',lease.name,mode='sql')
        answer=(result.get('result') or {}).get('rows') or []
        assert len(answer)==30000, ('full-result rows',len(answer),result.get('error'))
        assert len(answer[0])==12 and answer[0][0]=='sub_00000000' and answer[-1][0]=='sub_00029999'
        print('scale_full_result',json.dumps({'rows':len(answer),'seconds':round(time.perf_counter()-start,3),
                                            'bytes':len(json.dumps(result,default=str).encode())}),flush=True)
        print('PASS scale: 30000 rows, exact gold and complete output',flush=True)
    finally:
        lease.close()


if __name__=='__main__':
    main()
