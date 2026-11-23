"""Question-by-question source audit. Never modifies frozen questions or workbooks.

Calculations are authored from inspected source headers/rows, not by executing
the model-written answer programs. Each result retains workbook evidence.
"""
import csv, hashlib, json, math, re, subprocess, sys
sys.stdout.reconfigure(encoding='utf-8')
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from statistics import mean
from openpyxl import load_workbook
from openpyxl.utils.cell import range_boundaries, coordinate_to_tuple

ROOT = Path(__file__).resolve().parents[2]
QS = [json.loads(s) for s in (ROOT/'questions.jsonl').read_text(encoding='utf-8').splitlines()]
RESULTS = {}

@lru_cache(None)
def matrix(n, sheet=None):
    q = QS[n-1]
    wb = load_workbook(ROOT/q['workbook_file'], read_only=True, data_only=True)
    ws = wb[sheet or q['sheet']]
    data = list(ws.values)
    wb.close()
    return data

def cell(n, address, sheet=None):
    r,c = coordinate_to_tuple(address)
    return matrix(n,sheet)[r-1][c-1]

def vals(n, area, sheet=None):
    c1,r1,c2,r2 = range_boundaries(area)
    a = matrix(n,sheet)
    return [a[r-1][c-1] for r in range(r1,r2+1) for c in range(c1,c2+1)]

def numeric(v):
    return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)

def clean(v):
    return re.sub(r'\s+', ' ', str(v)).strip()

def label(v):
    return re.sub(r'\\\d.*$', '', clean(v)).strip()

def norm(v):
    return clean(v).casefold().replace('\u2013','-').replace('\u2014','-')

def equivalent(a,b):
    if numeric(a) and numeric(b): return math.isclose(a,b,rel_tol=1e-9,abs_tol=1e-8)
    if isinstance(a,list) and isinstance(b,list): return {norm(x) for x in a} == {norm(x) for x in b}
    return norm(a)==norm(b)

def audit(n, answer, reference, detail='', verdict=None):
    q = QS[n-1]
    v = verdict or ('Right' if equivalent(answer,q['gold']) else 'Wrong')
    detail = detail or f'Independently verified: {json.dumps(answer,ensure_ascii=False)}.'
    RESULTS[q['id']] = {'id':q['id'],'answer':q['gold'],'verdict':v,'comments':detail+' Source: '+reference+'.',
        'checked_answer':answer,'source_reference':reference,'workbook_file':q['workbook_file'],
        'sheet':q['sheet'],'question':q['question'],'domain':q['domain'],'kind':q['kind']}

def display_number(v):
    return f'{v:,.0f}' if float(v).is_integer() else f'{v:,.8f}'.rstrip('0').rstrip('.')

def rank(n, pairs, count, reference, reverse=True, detail=''):
    inputs=list(pairs)
    missing=[label(k) for k,v in inputs if not numeric(v)]
    pairs = [(label(k),v) for k,v in inputs if numeric(v)]
    pairs.sort(key=lambda kv:kv[1],reverse=reverse)
    selected=pairs[:count]
    tied=len(pairs)>count and pairs[count][1]==pairs[count-1][1]
    answer=[k for k,v in selected]
    extra='; '.join(f'{k}: {display_number(v)}' for k,v in pairs[:max(count+1,4)])
    audit(n,answer[0] if count==1 else answer,reference,(detail+' ' if detail else '')+extra+
          ('. Tied at the requested cutoff; the question has no tie-break rule.' if tied else '.'), 'Unsure' if tied else None)
    RESULTS[QS[n-1]['id']]['ranking_check']={'numeric_candidates':len(pairs),'missing_labels':missing,
        'leading_values':pairs[:max(count+1,4)],'tie_at_cutoff':tied}

@lru_cache(None)
def records(n):
    with (ROOT/QS[n-1]['workbook_file']).open(encoding='utf-8-sig',newline='') as f:
        return [{k.strip():v for k,v in r.items()} for r in csv.DictReader(f)]

def num(v):
    if numeric(v): return v
    try: return float(str(v).replace(',','').replace('%','').strip())
    except (ValueError,TypeError): return None

def tabular(n):
    a=matrix(n)
    return [dict(zip(a[0],r),_row=i+2) for i,r in enumerate(a[1:]) if any(x is not None for x in r)]

def frequencies(n,key,rs=None,normalize=False):
    rs=records(n) if rs is None else rs
    return Counter(clean(r[key]).upper() if normalize else r[key] for r in rs if r[key] is not None and clean(r[key]))

def findrows(n, text, col=0):
    return [(i+1,r) for i,r in enumerate(matrix(n)) if clean(r[col])==text]

def onlyrow(n,text):
    rows=findrows(n,text)
    assert len(rows)==1,(n,text,len(rows))
    return rows[0]

def statepairs(n,col):
    start=onlyrow(n,'Alabama')[0]
    end=onlyrow(n,'Wyoming')[0]
    rows=matrix(n)[start-1:end]
    assert len(rows)==51,(n,len(rows))
    return [(r[0],r[col-1]) for r in rows]

def choose(n,**filters):
    return [r for r in records(n) if all(r[k]==str(v) for k,v in filters.items())]

def one(n,**filters):
    matches=choose(n,**filters)
    assert len(matches)==1,(n,filters,len(matches))
    return matches[0]

def state_name(v):
    s=re.sub(r'\s*\(\d+\)', '', label(v)).lstrip('.').strip()
    return 'District of Columbia' if s=='Dist. of Col.' else s

def state_values(n,col,include_dc=True):
    names={state_name(r[0]) for r in matrix(113)[9:60]}
    if not include_dc: names.remove('District of Columbia')
    rows=[(state_name(r[0]),r[col-1]) for r in matrix(n) if state_name(r[0]) in names]
    assert len(rows)==len(names)==len(set(s for s,v in rows)),(n,len(rows),len(names))
    return rows

def run():
    audit(1,cell(1,'B10'),'A!B10','1970, all races, both sexes: 70.8 years.')
    audit(2,round(cell(1,'G7')-cell(1,'J7'),1),'A!G7,J7','48.7 - 33.5 = 15.2 years.')
    audit(3,mean(vals(1,'J187:J191')),'A!J187:J191','Mean of the five 2006-2010 Hispanic female estimates at age 75.')
    rank(4,[(cell(1,f'A{r}'),cell(1,f'E{r}')) for r in range(141,153)],1,'A!A141:A152,E141:E152')
    audit(5,cell(5,'BD6'),'A!BD4:BD6','2019 total: 32.5 million uninsured people under 65.')
    rank(6,[(cell(5,f'A{r}'),cell(5,f'BD{r}')) for r in range(93,97)],1,'A!A93:A96,BD93:BD96')
    rank(7,[(cell(5,f'A{r}'),cell(5,f'BD{r}')) for r in range(46,50)],2,'A!A46:A49,BD46:BD49')
    rank(8,[(label(h),v) for h,v in zip(matrix(5)[3][1:],matrix(5)[7][1:]) if re.match(r'^(19|20)\d\d',str(h))],1,'A!B4:BD4,B8:BD8',detail='Compared year estimates, excluding SE columns.')
    audit(9,cell(9,'B7'),'A!B7','Age-adjusted all-drug rate: 6.1 deaths per 100,000.')
    rank(10,[(cell(9,f'A{r}'),cell(9,f'T{r}')) for r in range(261,270)],1,'A!A256:T269',detail='All people, synthetic opioids excluding methadone, 2017.')
    assert cell(9,'T17') < cell(9,'T263')
    RESULTS['FF-0010']['comments']+=' The suppressed age-85+ component cannot exceed its all-drug rate of 4.0 (T17), below 19.5; it cannot change the maximum.'
    rank(11,[(label(h),v) for h,v in zip(matrix(9)[193][1:],matrix(9)[196][1:]) if re.match(r'^(19|20)\d\d',str(h))],1,'A!B194:X197',detail='2006 and 2007 both have the maximum crude rate of 1.8 per 100,000; 2006 is not unique.')
    rank(12,[(cell(9,f'A{r}'),cell(9,f'T{r}')) for r in range(135,144)],3,'A!A130:T143')
    audit(13,cell(13,'H90'),'A!H90','Hib full series, total, 2016: 80%.')
    rank(14,zip(vals(13,'B5:H5'),vals(13,'B7:H7')),1,'A!B5:H7',reverse=False)
    audit(15,'yes' if cell(13,'H54')>90 else 'no','A!H54',f'Polio coverage is {cell(13,"H54")}% > 90%.')
    rank(16,[(cell(13,f'A{r}'),cell(13,f'H{r}')) for r in range(56,63)],2,'A!A55:H62',detail='The White-only and Asian-only labels here are non-Hispanic groups; suppressed estimates excluded.')
    audit(17,cell(17,'AT62'),'A!AT62; footnote 5','37.7% is the workbook estimate. It is age-adjusted for ages 18-64; the question should ideally say age-adjusted.')
    rank(18,[(cell(17,f'A{r}'),cell(17,f'CN{r}')) for r in [20,21]],1,'A!CN20:CN21')
    rank(19,[(cell(17,f'A{r}'),cell(17,f'AT{r}')) for r in range(69,73)],2,'A!AT69:AT72')
    rank(20,zip(vals(17,'B5:AT5')[::2],vals(17,'B8:AT8')[::2]),1,'A!B5:AT5,B8:AT8',reverse=False)
    audit(21,cell(21,'G4'),'Table 1!G4','78.97103118896484 years, rounded to the stored gold precision.')
    rank(22,[(r[0],r[3]) for r in matrix(21)[3:] if numeric(r[3])],1,'Table 1!A4:D104')
    audit(23,'yes' if cell(21,'C84')>cell(21,'C4')/2 else 'no','Table 1!C4,C84',f'{cell(21,"C84"):g} of 100,000 survive to exact age 80 (>50,000).')
    rank(24,[(r[0],r[1]) for r in matrix(21)[3:] if numeric(r[1])],3,'Table 1!A4:B104',reverse=False)
    audit(25,cell(25,'G4'),'CA total population!G4','79.33352661132812 years, rounded to stored gold precision.')
    rank(26,[(cell(25,f'A{r}'),cell(25,f'D{r}')) for r in [24,34]],1,'CA total population!A24:D24,A34:D34')
    audit(27,'yes' if cell(25,'C84')>50000 else 'no','CA total population!C4,C84',f'{cell(25,"C84"):g} of 100,000 survive to exact age 80.')
    rank(28,[(r[0],r[3]) for r in matrix(25)[3:] if numeric(r[3])],3,'CA total population!A4:D104')
    hdr=matrix(29)[1]
    cols={int(y):i for i,y in enumerate(hdr) if numeric(y)}
    audit(29,matrix(29)[14][cols[2024]],'Table 3!AD15','2024 investment: $238.7 billion (current dollars).')
    rank(30,[(matrix(29)[r-1][0],matrix(29)[r-1][cols[2024]]) for r in [11,12]],1,'Table 3!AD11:AD12',detail='Medicaid funding, billions of dollars.')
    first=min(y for y,c in cols.items() if numeric(matrix(29)[8][c]) and matrix(29)[8][c]>1000)
    audit(31,str(first),'Table 3!B2:AD2,B9:AD9','First reported year with Medicare above $1,000 billion.')
    audit(32,sum(matrix(29)[14][cols[y]] for y in range(2020,2025)),'Table 3!Z15:AD15','Sum of the five displayed investment amounts for 2020-2024, billions of current dollars.')
    audit(33,cell(33,'B32'),'Table 16!B32','2024 total retail prescription drugs: $467 billion.')
    audit(34,'yes' if cell(33,'F28')>cell(33,'E28') else 'no','Table 16!E28:F28',f'Medicare {cell(33,"F28")} versus private insurance {cell(33,"E28")}, billions of dollars.')
    negatives=[r[0] for r in matrix(33)[33:61] if numeric(r[0]) and 2015<=r[0]<=2024 and numeric(r[2]) and r[2]<0]
    audit(35,len(negatives),'Table 16!A52:C61','Negative out-of-pocket annual growth years: '+', '.join(map(str,negatives))+'.')
    audit(36,round(cell(33,'E31')-cell(33,'F31'),1),'Table 16!E31:F31',f'{cell(33,"E31")} - {cell(33,"F31")} = 16.1 billion dollars.')
    audit(37,cell(37,'K10'),'MDCR HOSPICE 1_CPS_08UHS!K10','All beneficiaries, 2021, discharged dead: 1,257,831.')
    rank(38,statepairs(38,7),3,'MDCR HOSPICE 3_CPS_08UHS!A9:G59')
    sn=[(i,r) for i,r in enumerate(matrix(39)[5:15],6) if clean(r[0])=='Skilled Nursing']
    assert len(sn)==1
    audit(39,sn[0][1][2],f'MDCR HOSPICE 4_CPS_08UHS!C{sn[0][0]}','Skilled nursing service visits, total across all control types.')
    rank(40,[(r[0],r[1]) for r in matrix(40)[7:15]],1,'MDCR HOSPICE 6_CPS_08UHS!A8:B15')
    audit(41,cell(41,'E8'),'MDCR PREMIUMS 1_CPS_04PRM!E8','2021 standard base Part A premium amount: $3,898,888,074.')
    rank(42,[(r[0],r[2]) for r in matrix(42)[4:10]],1,'MDCR PREMIUMS 4_CPS_04PRM!A5:C10',reverse=False)
    race_start=onlyrow(43,'Non-Hispanic White')[0]
    race_end=onlyrow(43,'Unknown')[0]
    rank(43,[(r[0],r[9]) for r in matrix(43)[race_start-1:race_end]],1,f'MDCR PREMIUMS 5_CPS_04PRM!A{race_start}:J{race_end}')
    rank(44,statepairs(44,2),3,'MDCR PREMIUMS 9_CPS_04PRM!A8:B58',detail='Top states by Part D premium-paying beneficiaries.')
    audit(45,cell(45,'B10'),'MDCR ENROLL AB 33_CPS_02ENR!B10','2023 total includes Original Medicare and Medicare Advantage/other plans.')
    male=onlyrow(46,'Male');female=onlyrow(46,'Female')
    audit(46,'yes' if female[1][1]>male[1][1] else 'no',f'MDCR ENROLL AB 34_CPS_02ENR!B{male[0]},B{female[0]}',f'Female {female[1][1]:,}; male {male[1][1]:,}.')
    rank(47,zip(vals(45,'C3:N3'),vals(45,'C10:N10')),1,'MDCR ENROLL AB 33_CPS_02ENR!C3:N3,C10:N10')
    assert sum(vals(45,'C10:N10'))==cell(45,'B10')
    rank(48,statepairs(48,2),3,'MDCR ENROLL AB 35_CPS_02ENR!A9:B59')
    fl=onlyrow(49,'Florida');ca=onlyrow(49,'California')
    audit(49,'yes' if fl[1][9]>ca[1][9] else 'no',f'MDCR PROVIDERS 4_CPS_19PRV!J{fl[0]},J{ca[0]}',f'Florida {fl[1][9]:,}; California {ca[1][9]:,} short stay beds.')
    snf=onlyrow(50,'Skilled Nursing Facilities')[0]
    audit(50,matrix(50)[snf][6],f'MDCR PROVIDERS 2_CPS_19PRV!G{snf+1}','1,550,360 beds; also reconciles to provider-state table All Areas.')
    assert matrix(50)[snf][6]==cell(49,'D6')
    rank(51,[(cell(51,f'A{r}'),cell(51,f'G{r}')) for r in [34,35,36]],1,'MDCR PROVIDERS 3_CPS_19PRV!A34:G36')
    rank(52,[(r[0],r[5]) for r in matrix(52)[8:78]],5,'MDCR PROVIDERS 6_CPS_19PRV!A9:F78',detail='Physician specialty rows only; section labels and non-physicians excluded.')
    audit(53,round(cell(53,'K28')),'MDCR HHA 1_CPS_07UHH!K28',f'Underlying amount ${cell(53,"K28"):,.2f}; gold agrees rounded to whole dollars.')
    rank(54,[(r[0],r[10]) for r in matrix(53)[6:12]],1,'MDCR HHA 1_CPS_07UHH!A7:K12',reverse=False)
    rank(55,statepairs(55,11),3,'MDCR HHA 3_CPS_07UHH!A9:K59')
    audit(56,cell(56,'B14')+cell(56,'B15'),'MDCR HHA 6_CPS_07UHH!B14:B15','253,699 (50-99 visits) + 66,186 (100 or more) = 319,885 people.')
    injury=choose(57,Year=2017,**{'Cause Name':'Unintentional injuries'})
    ca=one(57,Year=2017,State='California',**{'Cause Name':'Unintentional injuries'})
    audit(57,int(ca['Deaths']),'CSV: Year=2017, State=California, Cause Name=Unintentional injuries','Unique matching record: 13,840 deaths.')
    rank(58,[(r['State'],float(r['Age-adjusted Death Rate'])) for r in injury if r['State'] in ['California','Florida']],1,'CSV: 2017 unintentional injuries, California/Florida, Age-adjusted Death Rate')
    kidney=int(one(57,Year=1999,State='United States',**{'Cause Name':'Kidney disease'})['Deaths'])
    audit(59,'yes' if kidney>35000 else 'no','CSV: 1999, United States, Kidney disease, Deaths',f'{kidney:,} nationwide deaths > 35,000; national total used once.')
    rank(60,[(r['State'],int(r['Deaths'])) for r in injury if r['State'] in ['California','Florida','New York']],2,'CSV: 2017 unintentional injuries, California/Florida/New York, Deaths')
    demo={'Sex':'Both Sexes','Age Group':'All Ages','Race and Hispanic Origin':'All Races-All Origins'}
    al=one(61,State='Alabama',Year=2016,**demo)
    audit(61,int(al['Deaths']),'CSV: Alabama, 2016, Both Sexes/All Ages/All Races-All Origins','756 deaths in the unique all-demographics record.')
    rank(62,[(state,float(one(61,State=state,Year=2012,**demo)['Age-adjusted Rate'])) for state in ['Alabama','Alaska']],1,'CSV: Alabama/Alaska, 2012, all demographics, Age-adjusted Rate')
    rank(63,[(r['Year'],float(r['Crude Death Rate'])) for r in choose(61,State='Alaska',**demo) if 2008<=int(r['Year'])<=2016],1,'CSV: Alaska, 2008-2016, all demographics, Crude Death Rate')
    wy=[int(one(61,State='Wyoming',Year=y,**demo)['Deaths']) for y in range(2014,2017)]
    audit(64,sum(wy),'CSV: Wyoming, 2014-2016, all demographics, Deaths',' + '.join(map(str,wy))+' = '+str(sum(wy))+' deaths.')
    audit(65,float(one(65,Year=2000,Race='All Races',Sex='Female')['Average Life Expectancy (Years)']),'CSV: 2000, All Races, Female, Average Life Expectancy (Years)','Unique matching estimate: 79.7 years.')
    total=choose(65,Race='All Races',Sex='Both Sexes')
    rank(66,[(r['Year'],float(r['Age-adjusted Death Rate'])) for r in total],1,'CSV: All Races/Both Sexes, all reported years, Age-adjusted Death Rate')
    before=[float(r['Age-adjusted Death Rate']) for r in total if int(r['Year'])<2005]
    audit(67,'yes' if min(before)<800 else 'no','CSV: All Races/Both Sexes, Year<2005, Age-adjusted Death Rate',f'Lowest pre-2005 rate is {min(before):g} per 100,000; 2005 excluded.')
    rank(68,[(r['Race'],float(r['Age-adjusted Death Rate'])) for r in choose(65,Year=2018,Sex='Both Sexes') if r['Race'] in ['Black','White']],1,'CSV: 2018, Both Sexes, Black/White, Age-adjusted Death Rate')
    audit(69,int(one(69,State='California')['Total Victims']),'CSV: California, Total Victims','64,115 victims in the supplied snapshot.')
    states=[r for r in records(69) if r['State'] not in ['National','Reporting States']]
    rank(70,[(r['State'],num(r['<1 Rate per 1,000 Children'])) for r in states],1,'CSV: state rows, <1 Rate per 1,000 Children')
    rank(71,[(r['State'],num(r['Total Victims'])) for r in states],3,'CSV: state rows, Total Victims')
    audit(72,float(one(69,State='National')['<1 Rate per 1,000 Children']),'CSV: National, <1 Rate per 1,000 Children','National infant victim rate: 25.7 per 1,000 children under 1.')
    audit(73,num(one(73,State='Iowa')['Pregnant Women Medicaid']),'CSV: Iowa, Pregnant Women Medicaid','375% of FPL in this frozen eligibility snapshot.')
    chip=[r['State'] for r in records(73) if num(r['Pregnant Women CHIP']) is not None]
    audit(74,sorted(chip),'CSV: Pregnant Women CHIP with numeric eligibility limits','Five states have numeric limits; N/A values are not program availability.')
    rank(75,[(s,num(one(73,State=s)['Medicaid Ages 6-18'])) for s in ['California','Texas']],1,'CSV: California/Texas, Medicaid Ages 6-18',detail='Limits expressed as percent of FPL.')
    al=one(73,State='Alaska')['Expansion to Adults']
    audit(76,'yes' if num(al) is not None else 'no','CSV: Alaska, Expansion to Adults',f'Expansion eligibility is listed at {al} of FPL in this snapshot.')
    mc=choose(77,Year=2024)
    audit(77,num(one(77,State='California',Year=2024)['Total Medicaid Enrollees']),'CSV: California, 2024, Total Medicaid Enrollees','14,904,950 Medicaid enrollees.')
    rank(78,[(r['State'],num(r['Medicaid Enrollment in Comprehensive Managed Care'])) for r in mc if r['State']!='TOTALS'],3,'CSV: 2024 state rows, Medicaid Enrollment in Comprehensive Managed Care')
    missing_mc=[r for r in mc if r['State']!='TOTALS' and num(r['Medicaid Enrollment in Comprehensive Managed Care']) is None]
    assert all(num(r['Total Medicaid Enrollees']) is not None and num(r['Total Medicaid Enrollees'])<3843890 for r in missing_mc)
    RESULTS['FF-0078']['comments']+=' Every row with an unavailable component has total Medicaid enrollment below 3,843,890, so it cannot enter the top three.'
    rank(79,[(r['Year'],num(r['Total Medicaid Enrollees'])) for r in choose(77,State='TOTALS') if 2016<=int(r['Year'])<=2024],1,'CSV: TOTALS, 2016-2024, Total Medicaid Enrollees')
    rank(80,[(r['State'],num(r['Medicaid Newly Eligible Adults Enrolled in Comprehensive MCOs'])) for r in mc if r['State']!='TOTALS'],1,'CSV: 2024 state rows, Medicaid Newly Eligible Adults Enrolled in Comprehensive MCOs')
    missing_adults=[r for r in mc if r['State']!='TOTALS' and num(r['Medicaid Newly Eligible Adults Enrolled in Comprehensive MCOs']) is None]
    assert all(num(r['Total Medicaid Enrollees']) is not None and num(r['Total Medicaid Enrollees'])<4723570 for r in missing_adults)
    RESULTS['FF-0080']['comments']+=' Every row with an unavailable component has total Medicaid enrollment below 4,723,570, so it cannot exceed California.'
    june={'Enrollment Year':2025,'Enrollment Month':6,'Updated Year':2026,'Updated Month':2}
    audit(81,num(one(81,State='Totals',**june)['Total Medicaid Enrollees']),'CSV: Totals, enrollment 2025-06, update 2026-02','82,877,280 in the unique matching national total; no summing of multiple releases.')
    sept={'Enrollment Year':2018,'Enrollment Month':9,'Updated Year':2020,'Updated Month':2}
    vt=num(one(81,State='Vermont',**sept)['Total VIII Group Newly Eligible Enrollees'])
    audit(82,'yes' if vt>0 else 'no','CSV: Vermont, enrollment 2018-09, update 2020-02, Newly Eligible',f'Newly eligible VIII group count is {vt:g}; zero is an explicit value.')
    trio=[(s,num(one(81,State=s,**sept)['Total VIII Group Newly Eligible Enrollees'])) for s in ['Ohio','New Jersey','Pennsylvania']]
    audit(83,sum(v for k,v in trio),'CSV: Ohio/New Jersey/Pennsylvania, enrollment 2018-09, update 2020-02, Newly Eligible','; '.join(f'{s}: {v:,.0f}' for s,v in trio)+'.')
    rank(84,frequencies(81,'Updated Year').items(),1,'CSV: Updated Year counts over all 7,854 records')
    cabg=one(85,**{'Hospital Name':'NYS - All Hospitals','Procedure':'CABG','Year of Hospital Discharge':'2019'})
    audit(85,float(cabg['Observed Mortality Rate']),'CSV: NYS - All Hospitals, CABG, 2019, Observed Mortality Rate','1.62% observed mortality; statewide row, not a mean of hospital rates.')
    valve=choose(85,Procedure='Valve or Valve/CABG',**{'Year of Hospital Discharge':'2013-2015'})
    rank(86,[(r['Hospital Name'],int(r['Number of Cases'])) for r in valve if r['Hospital Name'] in ['Mount Sinai Hospital','NYP-Weill Cornell']],1,'CSV: 2013-2015 Valve or Valve/CABG, Number of Cases')
    low=choose(85,**{'Comparison Results':'Rate significantly lower than Statewide Rate'})
    assert all(r['Hospital Name']!='NYS - All Hospitals' for r in low)
    audit(87,len(low),'CSV: Comparison Results=Rate significantly lower than Statewide Rate','93 hospital/procedure/period entries, not 93 distinct hospitals.')
    rank(88,frequencies(85,'Region').items(),2,'CSV: counts by Region across all entries')
    rank(89,frequencies(89,'Area Office').items(),1,'CSV: counts by Area Office across all 809 facility/bed-type records',detail='Counts records, as asked; one facility can have multiple bed-type records.')
    audit(90,len(choose(89,**{'Bed Type':'PEDVENTCAP'})),'CSV: Bed Type=PEDVENTCAP','Eight pediatric ventilator capacity entries.')
    raw_sum=sum(int(r['Available Capacity']) for r in records(89))
    residential=sum(int(r['Available Capacity']) for r in choose(89,**{'Bed Type':'NHBEDSAV'}))
    slots=sum(int(r['Available Capacity']) for r in choose(89,**{'Bed Type':'ADHCPSLOTSAV'}))
    audit(91,None,'CSV: Available Capacity grouped by Bed Type; NYSDOH census dictionary/overview',
          f'{raw_sum:,} is the raw column sum, but it is not a valid total of available beds: it includes {slots:,} adult day-care slots, 339 dialysis dens, and 341 pediatric ventilator capacity units. The publisher says pediatric ventilator capacity replaced its availability census. Residential NHBEDSAV availability alone is {residential:,}. Specify the bed category; do not sum these mixed categories. Definitions: https://health.data.ny.gov/api/views/izta-vnpq/files/485a00a4-0181-43e7-8bd0-7ddf818cbc95?download=true',verdict='Wrong')
    audit(92,'yes' if all(r['State']=='NY' for r in records(89)) else 'no','CSV: State across all 809 rows','Every record has State=NY; no missing state values.')
    ages=[num(r['Age']) for r in records(93) if num(r['Age']) is not None]
    audit(93,round(mean(ages),2),'CSV: Age across all records',f'Mean of {len(ages):,} recorded ages; {len(records(93))-len(ages)} missing ages excluded; rounded to 2 decimals.')
    rank(94,frequencies(93,'Residence County',normalize=True).items(),1,'CSV: Residence County counts; case/whitespace normalized; blanks excluded')
    rank(95,frequencies(93,'Residence City',normalize=True).items(),3,'CSV: Residence City counts; case/whitespace normalized; blanks excluded')
    audit(96,sum(r['Cocaine']=='Y' for r in records(93)),'CSV: Cocaine=Y','5,164 confirmed Y values; the remaining 7,799 entries are blank.')
    schools=records(97)
    enroll=[num(r['K_12_enrollment']) for r in schools if r['Reported']=='Y' and num(r['K_12_enrollment']) is not None]
    audit(97,sum(enroll),'CSV: Reported=Y, K_12_enrollment','2014-15 snapshot; summed enrollment only for reporting schools. Nonreporting records have blank enrollment.')
    rank(98,frequencies(97,'ESD').items(),1,'CSV: counts by ESD across all listed school records')
    p=sum(num(r['Number_exempt_for_pertussis']) or 0 for r in schools if r['Reported']=='Y')
    po=sum(num(r['Number_exempt_for_polio']) or 0 for r in schools if r['Reported']=='Y')
    audit(99,'yes' if p>po else 'no','CSV: Reported=Y, Number_exempt_for_pertussis/Number_exempt_for_polio',f'Pertussis {p:,.0f}; polio {po:,.0f} reported exemptions.')
    rank(100,frequencies(97,'County').items(),3,'CSV: County counts across all listed school records')
    rural=tabular(101)
    audit(101,sum(r['CAH']=='Yes' for r in rural),'in!E2:E360','37 facilities explicitly have CAH=Yes.')
    rank(102,[(r['FacilityName'],r['Discharges']) for r in rural if r['FacilityName'] in ['BARSTOW COMMUNITY HOSPITAL','BARTON MEMORIAL HOSPITAL']],1,'in!B2:K360, named hospitals')
    audit(103,sum(r['LicensedBeds'] for r in rural if numeric(r['LicensedBeds'])),'in!F2:F360','83,672 licensed beds across all 359 facility rows; no Rural=Yes restriction in the question.')
    audit(104,[r['FacilityName'] for r in rural if r['CountyName']=='El Dorado'],'in!B2:D360, CountyName=El Dorado','Exactly two matching hospitals.')
    ed=tabular(105)
    active=[r for r in ed if r['year']==2022 and r['Category']=='Active COVID-19']
    sutter=[r for r in active if r['FacilityName2']=='Sutter Amador Hospital']
    assert len(sutter)==1
    audit(105,sutter[0]['EDDXCount'],f'ED_COMBINE_AL!M{sutter[0]["_row"]}','2022 Active COVID-19 diagnosis count: 1,392. Used EDDXCount, not total ED visits.')
    rank(106,[(r['FacilityName2'],r['EDDXCount']) for r in active if r['FacilityName2'] in ['John Muir Medical Center – Walnut Creek Campus','John Muir Medical Center – Concord Campus']],1,'ED_COMBINE_AL: 2022, Active COVID-19, the two John Muir campuses, EDDXCount')
    rank(107,frequencies(105,'year',ed).items(),1,'ED_COMBINE_AL!E2:E12850',detail='Counts facility/category rows, not distinct hospitals.')
    audit(108,sorted(frequencies(105,'HospitalOwnership',ed)),'ED_COMBINE_AL!G2:G12850','Exactly three nonblank ownership labels.')
    hiv=[r for r in choose(109,Year=2013,**{'Race Ethnicity':'Hispanic'}) if r['Sex'] in ['M','Male'] and 'hiv' in r['Leading Cause'].lower()]
    assert len(hiv)==1
    audit(109,int(hiv[0]['Deaths']),'CSV: 2013, Hispanic, male, HIV disease, Deaths','Unique matching record: 122 deaths.')
    covid=[r for r in choose(109,Year=2021) if 'covid' in r['Leading Cause'].lower()]
    hm=[r for r in covid if r['Race Ethnicity']=='Hispanic' and r['Sex'] in ['M','Male']]
    bf=[r for r in covid if r['Race Ethnicity'] in ['Non-Hispanic Black','Black Non-Hispanic'] and r['Sex'] in ['F','Female']]
    assert len(hm)==len(bf)==1
    hrate=float(hm[0]['Age Adjusted Death Rate']);brate=float(bf[0]['Age Adjusted Death Rate'])
    audit(110,'yes' if hrate>brate else 'no','CSV: 2021 Covid-19, Hispanic male/Non-Hispanic Black female, Age Adjusted Death Rate',f'Hispanic males {hrate:g}; non-Hispanic Black females {brate:g} per 100,000.')
    rank(111,frequencies(109,'Year').items(),1,'CSV: Year counts across all 2,102 records')
    rank(112,frequencies(109,'Race Ethnicity').items(),2,'CSV: Race Ethnicity label counts',detail='Same top two even if synonymous Black/White labels are combined across years.')
    tx=onlyrow(113,'.Texas')
    audit(113,tx[1][5],f'NST-EST2023-POP!F{tx[0]}','Texas July 1, 2023 estimate: 30,503,301.')
    regions=[(s,onlyrow(113,s)[1][5]) for s in ['Northeast','Midwest','South','West']]
    rank(114,regions,3,'NST-EST2023-POP!A6:F9')
    rank(115,[(s,v) for s,v in regions if s in ['Northeast','Midwest']],1,'NST-EST2023-POP!F6:F7')
    pops=[(r[0],r[5]) for r in matrix(113)[9:60]]
    over=[s.lstrip('.') for s,v in pops if v>=10000000]
    audit(116,len(over),'NST-EST2023-POP!A10:F60','States at or above 10 million: '+', '.join(over)+'.')
    audit(117,cell(117,'F5'),'CO-EST2023-POP!F5','United States July 1, 2023 estimate: 334,914,895.')
    audit(118,cell(117,'F5')-cell(117,'C5'),'CO-EST2023-POP!C5,F5','334,914,895 - 331,526,933 = 3,387,962. Used July 1 estimates, not April 1 base.')
    bar=onlyrow(117,'.Barbour County, Alabama')
    audit(119,'yes' if bar[1][5]<bar[1][2] else 'no',f'CO-EST2023-POP!C{bar[0]}:F{bar[0]}',f'{bar[1][2]:,} in 2020 to {bar[1][5]:,} in 2023.')
    bibb=onlyrow(117,'.Bibb County, Alabama')
    rank(120,zip(vals(117,'C4:F4'),bibb[1][2:6]),1,f'CO-EST2023-POP!C{bibb[0]}:F{bibb[0]}')
    md=findrows(121,'Maryland')[0]
    audit(121,md[1][1],f'h08!B{md[0]}','2025 current-dollar median household income: $116,300.')
    rank(122,[(s,findrows(121,s)[0][1][1]) for s in ['California','Massachusetts']],1,'h08: current-dollar section, 2025 Median income column B')
    income=vals(121,'B10:J10')[::2]
    audit(123,mean(income),'h08!B10,D10,F10,H10,J10','Arithmetic mean of the five annual national medians (2021-2025): '+', '.join(f'{v:,}' for v in income)+'.')
    rank(124,zip(['2022','2021','2020','2019','2018'],vals(121,'H65:P65')[::2]),1,'h08!H65,J65,L65,N65,P65',detail='Constant 2025 dollars section, not current dollars.')
    cal=onlyrow(125,'California')
    audit(125,cal[1][1],f'pov21!B{cal[0]}','2023-2025 three-year poverty rate: 10.7%; margin-of-error column excluded.')
    pv=statepairs(125,2)
    rank(126,pv,1,'pov21!A7:B57')
    txp=onlyrow(125,'Texas')[1][1];nyp=onlyrow(125,'New York')[1][1]
    audit(127,'yes' if txp>nyp else 'no','pov21: Texas/New York, column B, 2023-2025',f'Texas {txp}%; New York {nyp}%.')
    high=[(s,v) for s,v in pv if v>15]
    audit(128,len(high),'pov21!A7:B57','Strictly above 15.0%: '+', '.join(f'{s} {v}%' for s,v in high)+'.')
    retail=findrows(129,'Retail and food services sales, total',1)[0]
    audit(129,retail[1][14],f'2025!O{retail[0]}','Full-year unadjusted total: 8,350,933 million dollars; reconciled to sum of 12 months.')
    assert retail[1][14]==sum(retail[1][2:14])
    furniture=findrows(130,'4421')[0];electronics=findrows(130,'443142')[0]
    rank(130,[(clean(furniture[1][1]),furniture[1][14]),(clean(electronics[1][1]),electronics[1][14])],1,f'2023!O{furniture[0]},O{electronics[0]}',detail='Unadjusted annual totals for NAICS 4421 and 443142.')
    food=findrows(131,'Food services and drinking places',1)[0]
    rank(131,zip([f'2020-{m:02d}' for m in range(1,13)],food[1][2:14]),1,f'2020!C{food[0]}:N{food[0]}',reverse=False)
    adj=findrows(132,'Retail and food services sales, total',1)[1]
    audit(132,mean(adj[1][2:14]),f'2022!C{adj[0]}:N{adj[0]}','Mean of 12 seasonally adjusted monthly totals, in millions of dollars; excludes the annual total column.')
    audit(133,cell(133,'G19'),'2022 ASFIN!G19','California motor fuel sales taxes: 8,638,712 thousand dollars.')
    statecols={clean(v):i for i,v in enumerate(matrix(133)[4])}
    tx=matrix(133)[10][statecols['Texas']];ny=matrix(133)[10][statecols['New York']]
    audit(134,'yes' if tx>ny else 'no','2022 ASFIN!row 11, Texas/New York columns',f'Total Taxes: Texas {tx:,}; New York {ny:,}, thousands of dollars.')
    rank(135,zip(matrix(133)[4][2:],matrix(133)[67][2:]),3,'2022 ASFIN!C68:AZ68',detail='General expenditure, Hospitals row; 50 states, US total excluded; thousands of dollars.')
    rank(136,[(s,matrix(133)[69][statecols[s]]) for s in ['Florida','Georgia']],1,'2022 ASFIN!K70:L70',detail='Highway expenditure, thousands of dollars.')
    audit(137,num(cell(137,'C18')),'22Lottery!C18','Florida prizes: 6,245,455 thousand dollars.')
    lottery=[r for r in matrix(137)[8:67] if r[0] is not None]
    assert len(lottery)==50
    rank(138,[(r[0],num(r[1])) for r in lottery],3,'22Lottery!A9:B67',detail='Ticket-sales income excluding commissions, as defined by the header.')
    no_lottery=[r[0] for r in lottery if r[1]=='-']
    audit(139,len(no_lottery),'22Lottery!A9:B67; note A69','No state-administered lottery: '+', '.join(no_lottery)+'. Dash meaning confirmed in source note.')
    rank(140,[(s,num(onlyrow(137,s)[1][3])) for s in ['California','Texas']],1,'22Lottery!D13,D59',detail='Administration expenses, thousands of dollars.')
    labor_header=matrix(141)[4]
    labor=[dict(zip(labor_header,r),_row=i+6) for i,r in enumerate(matrix(141)[5:]) if r[0] is not None]
    nation=[r for r in labor if r['FIPS_Code']=='00000'][0]
    rank(141,[(r['Area_Name'],r['Median_Household_Income_2022']) for r in labor if r['FIPS_Code'] in ['01000','53000']],1,'Unemployment Med HH Income: FIPS 01000/53000, Median_Household_Income_2022')
    rank(142,[(str(y),nation[f'Unemployment_rate_{y}']) for y in range(2019,2024)],1,'Unemployment Med HH Income: FIPS 00000, Unemployment_rate_2019 through _2023')
    metro=[r for r in labor if r['Metro_2023']==1]
    assert all(not r['FIPS_Code'].endswith('000') for r in metro)
    audit(143,len(metro),'Unemployment Med HH Income: Metro_2023=1','1,251 county/county-equivalent records; no state or national summary rows included.')
    audit(144,'yes' if nation['Unemployment_rate_2023']>4 else 'no','Unemployment Med HH Income: FIPS 00000, Unemployment_rate_2023',f'Nationwide annual unemployment rate is {nation["Unemployment_rate_2023"]:g}%, below 4.0%.')
    audit(145,cell(145,'D3'),'Rural-urban Continuum Code 2023!D3','Baldwin County, AL, 2020 Census population: 231,767.')
    rucc=tabular(145)
    audit(146,sum(r['RUCC_2023']==1 for r in rucc),'Rural-urban Continuum Code 2023!E2:E3236; Documentation!A13:B13','483 county/equivalent rows have code 1; the million-person threshold refers to the metro area, not each county.')
    audit(147,cell(147,'D30'),'Documentation!B30:D30','Martinsville City is combined with Henry County for this classification.')
    audit(148,[r[1] for r in matrix(147)[26:35] if r[3]=='Rockbridge County'],'Documentation!B32:D33','Buena Vista City and Lexington City.')
    food_data=[r for r in matrix(149)[2:] if numeric(r[0]) and r[0]>=1997]
    y2020=[r for r in food_data if r[0]==2020][0];y2019=[r for r in food_data if r[0]==2019][0]
    audit(149,y2020[8],'Nominal expenditures!I26','2020 food-at-home total: 860,098.13 million nominal dollars.')
    first=min(r[0] for r in food_data if r[18]>1000000)
    audit(150,str(first),'Nominal expenditures!A3:A31,S3:S31','First year with food-away-from-home expenditures strictly over 1,000,000 million dollars.')
    audit(151,'yes' if y2020[23]<y2019[23] else 'no','Nominal expenditures!X25:X26',f'2019 {y2019[23]:,.2f}; 2020 {y2020[23]:,.2f} million nominal dollars.')
    rank(152,[(r[0],r[9]) for r in food_data],3,'Nominal expenditures!A3:A31,J3:J31')
    texas=onlyrow(153,'Texas')
    audit(153,texas[1][17],f'Digest 2023 Table 203.20!R{texas[0]}','Texas fall 2022 actual enrollment: 5,519,599; adjacent footnote column excluded.')
    rank(154,[(s,onlyrow(153,s)[1][17]) for s in ['Northeast','Midwest']],1,'Digest 2023 Table 203.20: Northeast/Midwest, column R')
    rank(155,state_values(153,26),3,'Digest 2023 Table 203.20: state rows, column Z',detail='Projected percent change from 2022 to 2031, using underlying precision.')
    old=dict(state_values(153,10));new=dict(state_values(153,18))
    rising=[s for s in new if new[s]>old[s]]
    audit(156,len(rising),'Digest 2023 Table 203.20: state rows, columns J/R','Recomputed from 2017 and 2022 counts (not the percent-change field): '+', '.join(rising)+'.')
    ca=onlyrow(157,'California')
    audit(157,ca[1][17],f'Digest 2023 Table 236.65!R{ca[0]}','Unadjusted 2020-21 current expenditure per pupil; gold retains underlying precision.')
    rank(158,[(s,onlyrow(157,s)[1][35]) for s in ['Idaho','Utah']],1,'Digest 2023 Table 236.65: Idaho/Utah, column AJ')
    rank(159,state_values(157,36),4,'Digest 2023 Table 236.65: 50 states/DC, column AJ',reverse=False)
    fl=onlyrow(157,'Florida')
    audit(160,'yes' if fl[1][13]>10000 else 'no',f'Digest 2023 Table 236.65!N{fl[0]}',f'Florida unadjusted 2018-19 expenditure is ${fl[1][13]:,.2f} per pupil.')
    deg19=[(i+1,r) for i,r in enumerate(matrix(161)) if clean(r[0]).startswith('2019-20')][0]
    deg20=[(i+1,r) for i,r in enumerate(matrix(161)) if clean(r[0]).startswith('2020-21')][0]
    audit(161,deg19[1][9],f'Digest 2022 Table 318.10!J{deg19[0]}','2019-20 female bachelor degrees: 1,177,298.')
    audit(162,deg20[1][9]-deg20[1][7],f'Digest 2022 Table 318.10!H{deg20[0]},J{deg20[0]}',f'{deg20[1][9]:,} female - {deg20[1][7]:,} male = 344,917 degrees.')
    degree_rows=[(i+1,r) for i,r in enumerate(matrix(161)) if re.match(r'^\d{4}-\d{2}',clean(r[0]))]
    first=next((i,r) for i,r in degree_rows if numeric(r[16]) and numeric(r[18]) and r[18]>r[16]/2)
    audit(163,label(first[1][0]),f'Digest 2022 Table 318.10!Q{first[0]}:T{first[0]}','First reported academic year where female doctorate count exceeds half the total; recomputed from counts.')
    rank(164,[(label(r[0]),r[1]) for i,r in degree_rows if label(r[0]) in [f'{y}-{str(y+1)[-2:]}' for y in range(2010,2016)]],3,'Digest 2022 Table 318.10: 2010-11 through 2015-16, column B')
    grads=onlyrow(165,'2022')
    audit(165,grads[1][19],f'Digest 2023 Table 302.10!T{grads[0]}','Female recent high school completers enrolled in any college: approximately 66.03%; not the standard error.')
    audit(166,'yes' if grads[1][19]>grads[1][13] else 'no',f'Digest 2023 Table 302.10!N{grads[0]},T{grads[0]}',f'Female {grads[1][19]:.4f}%; male {grads[1][13]:.4f}%.')
    rank(167,[(clean(r[0]),r[7]) for r in matrix(165) if clean(r[0]).isdigit() and 2010<=int(r[0])<=2022],1,'Digest 2023 Table 302.10: years 2010-2022, column H')
    rank(168,[('Males',grads[1][15]),('Females',grads[1][21])],1,f'Digest 2023 Table 302.10!P{grads[0]},V{grads[0]}',detail='2-year-college enrollment percentages.')
    audit(169,onlyrow(169,'California')[1][15],'Digest 2023 Table 219.46!P12','California 2021-22 overall 4-year ACGR: 87%.')
    rank(170,[(s,onlyrow(169,s)[1][42]) for s in ['Texas','California']],1,'Digest 2023 Table 219.46!AQ12,AQ51',detail='English learner columns in the 2021-22 section, not 2020-21.')
    rank(171,state_values(169,16),3,'Digest 2023 Table 219.46!A8:P58')
    RESULTS['FF-0171']['verdict']='Unsure'
    RESULTS['FF-0171']['comments']='These are the top three published rates: West Virginia 91.2%, Tennessee 90.4%, Wisconsin 90.3%. New Mexico and Oklahoma are unavailable (---), so a ranking across all states cannot be confirmed. Specify states with reported rates. Source: Digest 2023 Table 219.46!P8:P58; missing P39,P44.'
    high=[(s,v) for s,v in state_values(169,16,include_dc=False) if numeric(v) and v>=90]
    audit(172,len(high),'Digest 2023 Table 219.46!P8:P58; P39,P44 unavailable',
          'Five states with reported rates are at least 90%: '+', '.join(f'{s} {v:g}%' for s,v in high)+'. New Mexico and Oklahoma are missing, so the count across all 50 states is unknown (between 5 and 7). Specify states with reported rates.',verdict='Unsure')
    vehicles={state_name(r[0]):r for r in matrix(173)[9:60]}
    audit(173,vehicles['Texas'][5],'CY MV-1 Publish: Texas, column F','Publicly owned buses: 56,923; private/commercial buses excluded.')
    rank(174,state_values(173,16),3,'CY MV-1 Publish!A10:P60',detail='All motor vehicles, total registrations.')
    rank(175,[(s,vehicles[s][9]) for s in ['Michigan','Pennsylvania']],1,'CY MV-1 Publish: Michigan/Pennsylvania, column J',detail='Total trucks, private/commercial plus publicly owned.')
    audit(176,'yes' if vehicles['Florida'][1]>vehicles['Texas'][1] else 'no','CY MV-1 Publish: Florida/Texas, column B',f'Florida {vehicles["Florida"][1]:,.0f}; Texas {vehicles["Texas"][1]:,.0f} private/commercial automobiles.')
    drivers=dict(state_values(177,17))
    audit(177,drivers['California'],'TOTAL!Q19','California all-ages licensed drivers: 27,632,103.')
    rank(178,list(drivers.items()),3,'TOTAL!A15:Q65')
    female=cell(177,'P66','FEMALES');male=cell(177,'P66','MALES')
    audit(179,round(abs(female-male),3),'FEMALES!P66; MALES!P66',f'Absolute difference of published underlying estimates: {female:.6f} - {male:.6f} = {abs(female-male):.6f}. Fractional counts arise from FHWA age-group estimates; about {abs(female-male):,.0f} drivers.')
    audit(180,cell(177,'U66'),'TOTAL!U66','Exactly age 16: 1,076,583 nationwide; under-16 excluded.')
    assert sum(v for v in vals(177,'U15:U65') if numeric(v))==cell(177,'U66')
    fuel={state_name(r[0]):r for r in matrix(181)[8:59]}
    audit(181,fuel['Texas'][12],'MF-21: Texas, column M','6,096,520 thousand gallons of special fuel, private/commercial highway use.')
    rank(182,state_values(181,18),3,'MF-21!A9:R59',detail='Total motor-fuel use, thousands of gallons.')
    rank(183,[(s,fuel[s][5]) for s in ['New York','Florida']],1,'MF-21: New York/Florida, column F',detail='Combined gasoline/gasohol, total highway use.')
    audit(184,'yes' if fuel['Texas'][17]>fuel['California'][17] else 'no','MF-21: Texas/California, column R',f'Texas {fuel["Texas"][17]:,}; California {fuel["California"][17]:,} thousand gallons.')
    airports=[r for r in tabular(185) if numeric(r['Rank'])]
    den=[r for r in airports if r['Airport Name']=='Denver International'][0]
    audit(185,den['CY 25 Enplanements'],'ChangeinRevenuePassengerEnplan !I5','Denver 2025 revenue passenger enplanements: 39,976,695.')
    rank(186,[(r['Airport Name'],r['CY 25 Enplanements']) for r in airports if r['Airport Name'] in ['Los Angeles International',"Chicago O'Hare International"]],1,'ChangeinRevenuePassengerEnplan !F4:I4,F6:I6')
    rank(187,[(r['Airport Name'],r['CY 25 Enplanements']) for r in airports if r['ST']=='TX'],3,'ChangeinRevenuePassengerEnplan : ST=TX, CY 25 Enplanements')
    hubs=[r['Airport Name'] for r in airports if r['ST']=='FL' and r['Hub']=='L']
    audit(188,len(hubs),'ChangeinRevenuePassengerEnplan : ST=FL, Hub=L',', '.join(hubs)+'.')
    returns=onlyrow(189,'Number of returns [1]')
    audit(189,returns[1][1],f'Sheet1!B{returns[0]}','2023 tax year, United States, all AGI brackets: 159,949,000 returns; count is not in thousands.')
    single=onlyrow(189,'Number of single returns');joint=onlyrow(189,'Number of joint returns')
    audit(190,'yes' if single[1][1]>joint[1][1] else 'no',f'Sheet1!B{single[0]},B{joint[0]}',f'Single {single[1][1]:,}; joint {joint[1][1]:,} returns.')
    efile=onlyrow(189,'Number of electronically filed returns')
    rank(191,zip(matrix(189)[4][2:12],efile[1][2:12]),1,f'Sheet1!C5:L5,C{efile[0]}:L{efile[0]}')
    eic=onlyrow(189,'Earned income credit: [10] Number')
    rank(192,zip(matrix(189)[4][2:12],eic[1][2:12]),3,f'Sheet1!C5:L5,C{eic[0]}:L{eic[0]}',detail='Number of returns claiming the credit, not credit dollar amount.')
    christmas=one(193,service_date='12/25/2007')
    audit(193,int(christmas['total_rides']),'CSV: service_date=12/25/2007, total_rides',f'{christmas["bus"]} bus + {christmas["rail_boardings"]} rail = {christmas["total_rides"]} rides.')
    assert int(christmas['total_rides'])==int(christmas['bus'])+int(christmas['rail_boardings'])
    cta=records(193)
    assert len(cta)==len(set(r['service_date'] for r in cta))
    day_counts=frequencies(193,'day_type')
    audit(194,day_counts['A'],'CSV: day_type=A','1,335 service dates. Date uniqueness checked across all 9,374 rows.')
    rank(195,day_counts.items(),1,'CSV: counts of distinct service_date by day_type')
    audit(196,sorted(day_counts),'CSV: distinct nonblank day_type values','Exactly A, U, and W.')
    housing=records(197)
    units=[num(r['Units']) for r in housing if num(r['Units']) is not None]
    audit(197,sum(units),'CSV: Units across all 598 property listings',
          '29,550 is the sum of 597 reported unit counts, matching the question\'s "units recorded" wording. Arthur on Aberdeen (210 N. Aberdeen St.) has a blank Units value. The sum is correct for recorded units; the complete physical total across all 598 properties is unknown.')
    communities=Counter(clean(r['Community Area Name']) for r in housing if clean(r['Community Area Name']))
    rank(198,communities.items(),1,'CSV: property listing counts by Community Area Name')
    audit(199,'yes' if communities['Uptown']>communities['North Lawndale'] else 'no','CSV: Community Area Name=Uptown/North Lawndale',f'Uptown {communities["Uptown"]} listings; North Lawndale {communities["North Lawndale"]}.')
    companies=Counter(clean(r['Management Company']) for r in housing if clean(r['Management Company']))
    rank(200,companies.items(),3,'CSV: listing counts by Management Company; whitespace normalized')
    lirr='LIRR: Total Estimated Ridership';mn='Metro-North: Total Estimated Ridership'
    audit(201,int(one(201,Date='01/01/2025')[lirr]),'CSV: Date=01/01/2025, LIRR: Total Estimated Ridership','90,318 estimated LIRR rides.')
    d2024=one(201,Date='01/01/2024')
    audit(202,int(d2024[lirr])-int(d2024[mn]),'CSV: Date=01/01/2024, LIRR and Metro-North total ridership',f'{d2024[lirr]} - {d2024[mn]} = 8,854 rides.')
    mta=records(201)
    assert len(mta)==len(set(r['Date'] for r in mta))
    zero=[r for r in mta if num(r['Staten Island Railway: Total Estimated Ridership'])==0]
    audit(203,len(zero),'CSV: Staten Island Railway: Total Estimated Ridership=0','32 explicit zero-ridership dates; missing values are not zero. Dates are unique.')
    rank(204,[(str(y),int(one(201,Date=f'01/01/{y}')['Subways: Total Estimated Ridership'])) for y in range(2021,2026)],1,'CSV: January 1 of 2021-2025, Subways: Total Estimated Ridership')
    markets=records(205)
    snap=sum(r['SNAP']=='Y' for r in markets);fmnp=sum(r['FMNP']=='Y' for r in markets)
    audit(205,snap,'CSV: SNAP=Y','298 directory listings accept SNAP; the source directory includes markets and farm stands.')
    rank(206,frequencies(205,'County').items(),1,'CSV: directory listing counts by County')
    rank(207,frequencies(205,'City').items(),3,'CSV: directory listing counts by City',detail='Uses city labels as supplied (New York and Brooklyn are separate directory labels).')
    audit(208,fmnp-snap,'CSV: FMNP=Y and SNAP=Y',f'{fmnp} FMNP listings - {snap} SNAP listings = {fmnp-snap}; not the count of FMNP-only listings.')
    june26=one(209,**{'Calendar Year':2026,'Month':'June'})
    audit(209,float(june26['Total Revenue']),'CSV: Calendar Year=2026, Month=June, Total Revenue',f'Exact source amount is ${float(june26["Total Revenue"]):,.2f}; gold serialization is rounded to 10 significant digits.')
    tax=records(209)
    cannabis=[r for r in tax if num(r['Cannabis Tax']) is not None]
    audit(210,len(cannabis),'CSV: nonblank numeric Cannabis Tax records',f'{len(cannabis)} monthly entries with recorded values, including {sum(num(r["Cannabis Tax"])==0 for r in cannabis)} explicit zero. There are 41 months with positive collections; gold counts recorded values.')
    tax12=choose(209,**{'Calendar Year':2012})
    assert len(tax12)==12
    withholding=sum(num(r['Withholding']) or 0 for r in tax12);sales=sum(num(r['Sales and Use']) or 0 for r in tax12)
    rank(211,[('Withholding',withholding),('Sales and Use',sales)],1,'CSV: Calendar Year=2012 (12 months), Withholding/Sales and Use')
    # DRS July accrual schedule, June final statement, and December fiscal YTD controls.
    tax12_by_month={r['Month']:r for r in tax12}
    assert len(tax12_by_month)==12
    fiscal_second_half={'August','September','October','November','December'}
    for field,june_final,december_ytd in [
        ('Withholding','851135077.96','1956093610.00'),
        ('Sales and Use','661011335.65','1510134310.53'),
    ]:
        assert tax12_by_month['July'][field].strip()==''
        assert Decimal(tax12_by_month['June'][field])==Decimal(june_final)
        assert sum(Decimal(r[field]) for r in tax12 if r['Month'] in fiscal_second_half)==Decimal(december_ytd)
    RESULTS['FF-0211']['comments']=(
        f'Right on the published accounting basis: withholding ${withholding:,.2f} exceeds sales and use ${sales:,.2f}. '
        'July 2012 has blank cells for both columns because $477,113,000 of withholding and $438,055,000 of sales/use receipts '
        'were accrued to the fiscal year ending June 30, 2012 and are already included in June\'s final figures. '
        'Adding those July amounts again would double-count them. August-December sums also match December\'s fiscal YTD totals exactly. '
        'Source: CSV, Calendar Year=2012, Withholding/Sales and Use. '
        'July accrual schedule (p. 2): https://portal.ct.gov/-/media/drs/research/12comparstate/monstmtjul2012revisedpdf.pdf ; '
        'June final statement (p. 1): https://portal.ct.gov/-/media/drs/research/12comparstate/monstmtjun2012finalpdf.pdf ; '
        'December YTD statement (p. 1): https://portal.ct.gov/-/media/drs/research/12comparstate/monstmtdec2012pdf.pdf')
    cigarettes=[num(r['Cigarette Tax']) for r in tax if num(r['Cigarette Tax']) is not None]
    audit(212,'yes' if min(cigarettes)<2000000 else 'no','CSV: Cigarette Tax across all numeric reported months',f'Minimum recorded cigarette-tax revenue is ${min(cigarettes):,.2f}; blanks excluded.')
    squirrels=records(213)
    audit(213,sum(r['Shift']=='AM' for r in squirrels),'CSV: Shift=AM','1,347 morning sightings; counts observations, not an estimate of unique animals.')
    colors=frequencies(213,'Primary Fur Color')
    ordered=colors.most_common()
    audit(214,ordered[1][0],'CSV: counts of nonblank Primary Fur Color','; '.join(f'{k}: {v}' for k,v in ordered)+'.')
    assert ordered[0][1]>ordered[1][1]>ordered[2][1]
    dates=frequencies(213,'Date')
    rank(215,[(datetime.strptime(s,'%m%d%Y').date().isoformat(),v) for s,v in dates.items()],1,'CSV: sighting counts by Date parsed as MMDDYYYY')
    rank(216,frequencies(213,'Hectare').items(),3,'CSV: sighting counts by Hectare')

def save():
    assert set(RESULTS)=={q['id'] for q in QS}, 'Every question must have exactly one review.'
    assert len(RESULTS)==len(QS)==216
    assert all(r['verdict'] in {'Right','Wrong','Unsure'} and r['comments'].strip() for r in RESULTS.values())
    manifest=json.loads((ROOT/'manifest.json').read_text(encoding='utf-8'))
    hashes={s['file']:hashlib.sha256((ROOT/s['file']).read_bytes()).hexdigest() for s in manifest['sources']}
    mismatches=[s['file'] for s in manifest['sources'] if s['sha256']!=hashes[s['file']]]
    assert not mismatches, mismatches
    qhash=hashlib.sha256((ROOT/'questions.jsonl').read_bytes()).hexdigest()
    assert qhash==manifest['questions_file']['sha256']
    replay_path=ROOT/'work/audit/program-replay.json'
    replay=json.loads(replay_path.read_text(encoding='utf-8'))
    assert replay['questions_sha256']==qhash
    assert replay['count']==replay['executed']==replay['matching_gold']==2*len(QS)
    assert not replay['failures']
    replay_by_id=defaultdict(dict)
    for item in replay['results']:
        replay_by_id[item['id']][item['program']]=item
    for q in QS:
        assert set(replay_by_id[q['id']])=={'program','independent_check_program'}
        for field,item in replay_by_id[q['id']].items():
            assert item['program_sha256']==hashlib.sha256(q[field].encode()).hexdigest()
        RESULTS[q['id']]['supplied_program_replay']={k:{a:v for a,v in item.items() if a not in {'id','program'}} for k,item in replay_by_id[q['id']].items()}
    repo=Path(__file__).resolve().parents[5]
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
    dirty=subprocess.check_output(['git','status','--porcelain'],cwd=repo,text=True).splitlines()
    artifacts=['manifest.json','work/audit/independent_audit.py','work/audit/replay_programs.py',
               'work/audit/program-replay.json','scripts/common.py','scripts/build_outputs.py']
    output={'dataset':manifest['name'],'reviewed_at_utc':datetime.now(timezone.utc).isoformat(),
            'questions_sha256':qhash,'source_hashes':hashes,
            'method':'Individual question semantic review with independent direct-cell and raw-row calculations. Frozen inputs preserved. No serving or training evaluation.',
            'verdict_policy':{'Right':'The gold answer is supported by the frozen source and the stated question, with any rounding or interpretation noted.',
                              'Wrong':'The gold answer is not a valid answer to the stated question.',
                              'Unsure':'A tie, missing data, or ambiguity prevents a unique or complete answer to the stated question.'},
            'numeric_comparison':'1e-9 relative / 1e-8 absolute for serialization rounding; explicit question rounding applied where needed.',
            'provenance':{'code_commit':commit,'dirty_worktree':bool(dirty),'git_status_porcelain':dirty,
                          'repository':str(repo),'audit_script':'work/audit/independent_audit.py',
                          'configuration':{'python':sys.version,'source_access':'read-only, openpyxl data_only plus raw CSV',
                                           'source_hashes_match_manifest':True,'serving_evaluation':False,'training':False},
                          'artifact_sha256':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in artifacts}},
            'program_reproduction':{'count':replay['count'],'executed':replay['executed'],'matching_gold':replay['matching_gold'],
                                    'identical_primary_and_check_program_pairs':sum(replay_by_id[q['id']]['program']['program_sha256']==replay_by_id[q['id']]['independent_check_program']['program_sha256'] for q in QS),
                                    'note':'Reproduction checks supplement the source audit; agreement alone does not establish semantic correctness.'},
            'counts':dict(Counter(r['verdict'] for r in RESULTS.values())),
            'reviewed_count':len(RESULTS),'total_questions':len(QS),'workbook_count':len(hashes),
            'results':[RESULTS[k] for k in sorted(RESULTS)]}
    (ROOT/'review.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    with (ROOT/'review.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f)
        writer.writerow(['ID','Answer','Verdict','Comments','Question','Workbook','Sheet','Independently checked answer'])
        for r in output['results']:
            answer=r['answer'] if isinstance(r['answer'],str) else json.dumps(r['answer'],ensure_ascii=False)
            checked='' if r['checked_answer'] is None else r['checked_answer'] if isinstance(r['checked_answer'],str) else json.dumps(r['checked_answer'],ensure_ascii=False)
            writer.writerow([r['id'],answer,r['verdict'],r['comments'],r['question'],r['workbook_file'],r['sheet'] or '',checked])
    sys.path.insert(0,str(ROOT/'scripts'))
    from build_outputs import write_html
    write_html(QS)
    (ROOT/'work/audit/review-progress.json').unlink(missing_ok=True)
    print(json.dumps({'reviewed':len(RESULTS),'counts':output['counts'],'issues':[r for r in RESULTS.values() if r['verdict']!='Right']},ensure_ascii=False,indent=2))

if __name__=='__main__':
    run()
    save()
