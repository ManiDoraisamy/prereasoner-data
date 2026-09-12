"""The browser release gate must not invent a second gold definition."""
from tests.test_datasets import grade_answer


def main():
    scalar=lambda x:{'result':{'columns':['amount'],'rows':[[x]]}}
    assert grade_answer(scalar('1509.36'),1509.36) is None
    assert grade_answer(scalar('1509.365'),1509.36) is not None
    assert grade_answer(scalar('1509.365'),1509.36,followup=True) is None
    assert grade_answer(scalar(108),100,fx=True) is None
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
    print('dataset gold: 14 checks passed')


if __name__=='__main__':
    main()
