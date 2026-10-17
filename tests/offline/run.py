"""Run actual BIT components offline; fail on assertions, not just subprocess exit.
Usage: python3 tests/offline/run.py --out /tmp/bit-offline-unique
Requires project venv, Linux libseccomp. Never reads production configuration.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--out', required=True)
parser.add_argument('--python', default=str(ROOT / 'venv/bin/python'))
a = parser.parse_args()
E = Path(a.out).resolve()
if E.exists():
    parser.error('Use a new evidence directory (existing evidence is never overwritten)')
if E == ROOT or ROOT in E.parents:
    parser.error('Evidence must be outside repository')
E.mkdir(parents=True)
PY = Path(a.python).absolute()
env = {'PATH':str(PY.parent)+':/usr/bin:/bin', 'HOME':str(E),
       'PYTHONPATH':str(E/'guard')+':'+str(E/'isolated'),
       'PYTHONDONTWRITEBYTECODE':'1', 'BIT_EVIDENCE_ROOT':str(E),
       'BIT_PRODUCTION_ROOT':str(ROOT), 'BIT_VENV_ROOT':str(PY.parent.parent),
       'PIP_NO_INDEX':'1', 'LC_ALL':'C.UTF-8'}
# Source-only staging runs before runtime denial; prepare has an explicit allowlist.
subprocess.run(['/usr/bin/python3',str(HERE/'no_network.py'),'/usr/bin/python3',str(HERE/'prepare.py')],
               env={k:v for k,v in env.items() if k!='PYTHONPATH'},check=True)
results=[]
def run(label,args):
    cmd=['/usr/bin/python3',str(E/'no_network.py'),str(PY)]+args
    with (E/(label+'.log')).open('w') as log:
        try:
            rc=subprocess.run(cmd,cwd=E/'isolated',env=env,stdout=log,stderr=subprocess.STDOUT,timeout=240).returncode
        except subprocess.TimeoutExpired:
            rc=124
    result={'label':label,'command':cmd,'exit':rc}
    results.append(result)
    print(json.dumps(result),flush=True)
    return rc
probe="""import socket,os
s=socket.socket()
try: s.connect(('127.0.0.1',9))
except PermissionError: print('kernel connect EPERM')
else: raise AssertionError('network allowed')
try: open(os.environ['BIT_PRODUCTION_ROOT']+'/config/config.py')
except PermissionError: print('production config read denied')
else: raise AssertionError('config allowed')
"""
if run('guard-probe',['-c',probe]):
    raise SystemExit('Safety probe failed; refusing test execution')
for period in ['long','short']:
    out=E/'runs'/period
    run('oracles-'+period,[str(E/'oracles.py'),period,str(out)])
    if not (out/'oracles.json').exists():
        results[-1]['failed']=['Missing oracle evidence']
        continue
    data=json.loads((out/'oracles.json').read_text())
    checks=data['checks']
    results[-1]['checks']=len(checks)
    results[-1]['failed']=[c['name'] for c in checks if not c['passed']]
    results[-1]['unhit_rules']=[r for r in data['rules'] if r.get('hit',0)==0]
    results[-1]['harness_errors']=sum(bool(t['harness_exc']) for t in data['timeline'])
# 下列用例位于 bit/B_TEST/，该目录不随公开仓库发布。
# 缺失时记为 skipped 并继续，不计入失败，以免公开仓库的离线套件必然报错。
_BTEST = E/'isolated/bit/B_TEST'
skipped=[]
for name in ['test_model_integration','test_model_switching','test_switch_model1_to_model2',
             'test_switch_model2_to_model1','test_bit_position_lifecycle','test_time_handling',
             'test_strategy_time_integration']:
    script = _BTEST/(name+'.py')
    if not script.is_file():
        skipped.append(name)
        continue
    run(name,[str(script)])
if skipped:
    print('SKIPPED (bit/B_TEST not present in this distribution): ' + ', '.join(skipped))
(E/'results.json').write_text(json.dumps({'results':results,'skipped':skipped},indent=2))
failed=any(r['exit'] or r.get('failed') or r.get('unhit_rules') or r.get('harness_errors') for r in results)
print(json.dumps({'passed':not failed,'skipped':skipped,'results':results},indent=2))
sys.exit(1 if failed else 0)
