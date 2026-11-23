"""Read-only source inspection for the frozen-answer audit."""
import csv, json, sys
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
from openpyxl import load_workbook
from openpyxl.utils.cell import range_boundaries, get_column_letter

ROOT = Path('C:/work/datasets/fresh-frozen-20261110')
QS = [json.loads(s) for s in (ROOT/'questions.jsonl').read_text(encoding='utf-8').splitlines()]

def show_questions(start, end, programs=False):
    for q in QS[start-1:end]:
        print(q['id'], q['workbook_file'], 'SHEET', q['sheet'])
        print('QUESTION', q['question'])
        print('GOLD', json.dumps(q['gold'], ensure_ascii=False))
        if programs:
            print(q['program'])
        print()

def show_sheet(qnum, ranges):
    q = QS[qnum-1]
    path = ROOT/q['workbook_file']
    if path.suffix == '.csv':
        with path.open(encoding='utf-8-sig', newline='') as f:
            rows = list(csv.reader(f))
        print('CSV', path.name, 'rows', len(rows)-1)
        for n in ([0,1,2,len(rows)-1] if not ranges else [int(x)-1 for x in ranges]):
            print(n+1, json.dumps(rows[n], ensure_ascii=False))
        return
    wb = load_workbook(path, read_only=True, data_only=True)
    print(path.name, 'SHEETS', wb.sheetnames)
    ws = wb[q['sheet']]
    print('SHEET', ws.title, 'DIMENSIONS', ws.max_row, ws.max_column)
    for area in ranges or ['A1:AZ16']:
        a,b,c,d = range_boundaries(area)
        for row in ws.iter_rows(min_row=b,max_row=d,min_col=a,max_col=c):
            vals = [f'{get_column_letter(col)}{rownum}={str(cell.value).strip()}' for rownum,col,cell in [(cell.row,cell.column,cell) for cell in row if cell.value is not None]]
            if vals: print(' | '.join(vals))
    wb.close()

if __name__ == '__main__':
    if sys.argv[1] == 'q': show_questions(int(sys.argv[2]),int(sys.argv[3]),'--programs' in sys.argv)
    elif sys.argv[1] == 'sheet': show_sheet(int(sys.argv[2]),sys.argv[3:])
    elif sys.argv[1] == 'headers':
        seen=set()
        for n in range(int(sys.argv[2]),int(sys.argv[3])+1):
            q=QS[n-1]
            key=(q['workbook_file'],q['sheet'])
            if key not in seen:
                seen.add(key)
                show_sheet(n,[] if q['sheet'] is None else ['A1:Z6'])
