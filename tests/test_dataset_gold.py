"""The browser release gate must not invent a second gold definition."""
from tests.test_datasets import grade_answer
from regress.browser_gold import grade as grade_browser_case


def main():
    scalar=lambda x, realization=None: {
        'result':{'columns':['amount'],'rows':[[x]]},
        **({'currency':{'status':'satisfied','realization':realization}} if realization else {}),
    }
    assert grade_answer(scalar('1509.36'),1509.36) is None
    assert grade_answer(scalar('1509.365'),1509.36) is not None
    assert grade_answer(scalar('1509.365'),1509.36,followup=True) is None
    assert grade_answer(scalar(108,'converted'),100,fx=True) is None
    assert grade_answer(scalar(100,'currency_filter'),100,fx=True) is not None
    assert grade_answer(scalar(100),100,fx=True) is not None
    assert grade_answer(scalar(100,'identity'),100,fx=True) is None
    assert grade_answer(scalar(120),100,fx=True) is not None
    assert grade_answer(scalar('NaN'),100) is not None
    assert grade_answer(scalar('Infinity'),100) is not None
    assert grade_answer({'result':{'rows':[['A'],['B']]}}, [['A'],['B']]) is None
    assert grade_answer({'result':{'rows':[['B'],['A']]}}, [['A'],['B']]) is not None
    assert grade_answer({'clarify':True},None) is None
    assert grade_answer(scalar(0),None) is not None
    assert grade_answer({'result':{'columns':['amount'],'rows':[[3],[4]]}},3) is not None
    assert grade_answer({'result':{'columns':['customer','extra'],'rows':[['A',1]]}},
                        {'columns':['customer'],'rows':[['A']]}) is None
    assert grade_answer({'result':{'columns':['wrong'],'rows':[['A']]}},
                        {'columns':['customer'],'rows':[['A']]}) is not None
    fx_item = {
        'expected': 1925, 'fx': True, 'response': {
            'result': {'columns': ['total'], 'rows': [[1925]]},
            'currency': {'status': 'satisfied', 'realization': 'converted', 'target': 'GBP'},
            'assistant_reply': 'For all of Europe, your total is about £1,925 GBP.',
        },
    }
    assert grade_browser_case(fx_item)['passed'] is True
    assert not grade_browser_case({**fx_item, 'response': {
        **fx_item['response'],
        'assistant_reply': 'For all of Europe, your total is about £810 GBP.',
    }})['passed']
    assert not grade_browser_case({**fx_item, 'response': {
        **fx_item['response'], 'assistant_reply': 'For all of Europe, your total is $1,925 USD.',
    }})['passed']
    assert not grade_browser_case({**fx_item, 'response': {
        **fx_item['response'], 'result': {'columns': ['total'], 'rows': [[2102]]},
        'currency': {'status': 'satisfied', 'realization': 'raw_sum', 'target': 'GBP'},
        'assistant_reply': 'For all of Europe, your total is about £2,102 GBP.',
    }})['passed']
    assert not grade_browser_case({**fx_item, 'response': {
        **fx_item['response'], 'assistant_reply': 'I cannot convert this amount to GBP.',
    }})['passed']
    assert not grade_browser_case({**fx_item, 'response': {
        **fx_item['response'],
        'assistant_reply': 'The earlier figure was £1,925 GBP, but the total is £810 GBP.',
    }})['passed']
    assert not grade_browser_case({**fx_item, 'response': {
        **fx_item['response'],
        'assistant_reply': 'The total is 1,925, expressed in GBP.',
    }})['passed']
    assert grade_browser_case({**fx_item, 'response': {
        **fx_item['response'],
        'assistant_reply': 'GBP total: 1,925.',
    }})['passed']
    print('dataset gold: 25 checks passed')


if __name__=='__main__':
    main()
