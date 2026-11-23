"""Reproduce both supplied programs for every frozen question using the existing runner."""
import ast, hashlib, json, sys
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
ROOT=Path('C:/work/datasets/fresh-frozen-20261110')
sys.path.insert(0,str(ROOT/'scripts'))
sys.path.insert(0,str(ROOT/'work/audit'))
import common
from independent_audit import equivalent, QS

common.PY=sys.executable
common.USER_SITE=''
original_rmtree=common.shutil.rmtree
tmp_root=Path(common.TMP).resolve()
assert tmp_root==ROOT.resolve()/'work'/'tmp'
def checked_rmtree(path,*args,**kwargs):
    target=Path(path).resolve()
    assert target.is_relative_to(tmp_root) and target!=tmp_root,(target,tmp_root)
    return original_rmtree(target,*args,**kwargs)
common.shutil.rmtree=checked_rmtree

def replay(q,field):
    program=q[field]
    tree=ast.parse(program)
    for node in ast.walk(tree):
        if isinstance(node,ast.Import):
            assert all(n.name.split('.')[0] in common.ALLOWED_IMPORTS for n in node.names)
        if isinstance(node,ast.ImportFrom):
            assert node.module and node.module.split('.')[0] in common.ALLOWED_IMPORTS
        if isinstance(node,ast.Attribute):
            assert not node.attr.startswith('__')
        if isinstance(node,ast.Name):
            assert not node.id.startswith('__')
    ok,value,error=common.run_program(program,Path(q['workbook_file']).name)
    if isinstance(value,bool): value='yes' if value else 'no'
    return {'id':q['id'],'program':field,'ok':ok,'value':value,'error':error,
            'matches_gold':ok and equivalent(value,q['gold']),
            'program_sha256':hashlib.sha256(program.encode()).hexdigest()}

if __name__=='__main__':
    output_path=ROOT/'work/audit/program-replay.json'
    results=[]
    def save():
        ordered=sorted(results,key=lambda r:(r['id'],r['program']))
        failures=[r for r in ordered if not r['ok'] or not r['matches_gold']]
        data={'runner':'scripts/common.py run_program; bundled Python; 4 isolated workers; existing network block and static import restrictions plus AST checks',
              'updated_at_utc':datetime.now(timezone.utc).isoformat(),
              'questions_sha256':hashlib.sha256((ROOT/'questions.jsonl').read_bytes()).hexdigest(),
              'count':len(ordered),'executed':sum(r['ok'] for r in ordered),'matching_gold':sum(r['matches_gold'] for r in ordered),'failures':failures,'results':ordered}
        output_path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        return data
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs=[pool.submit(replay,q,k) for q in QS for k in ['program','independent_check_program']]
        for job in as_completed(jobs):
            results.append(job.result())
            if len(results)%18==0: save()
            if len(results)%54==0: print(f'Reproduced {len(results)}/432 supplied calculations',flush=True)
    data=save()
    print(json.dumps({k:v for k,v in data.items() if k!='results'},ensure_ascii=False,indent=2))
