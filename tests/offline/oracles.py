"""Deterministic extra oracles on the recovered real-component driver."""
import runpy, sys, json
from pathlib import Path
period,out=sys.argv[1:]
sys.argv=[sys.argv[0],period,'oracles',out]
g=runpy.run_path(str(Path(__file__).with_name('drive_bit_offline.py')),run_name='offline_driver')
SIM=g['SIM']; checks=[]
def check(name,condition,detail=None):
 checks.append({'name':name,'passed':bool(condition),'detail':detail})
def reset():
 SIM.pos={'LONG':0.0,'SHORT':0.0};SIM.algo={};SIM.rules=[];g['clean_state']();g['set_model'](1)
 # Exact quantity fixture; isolates state logic from quantity truncation.
 for cfg in [g['LONG_TERM_CONFIG'],g['SHORT_TERM_CONFIG']]: cfg['position_size']=100;cfg['signal_config']['position_correction_enabled']=False
 g['BASE'].loc[:,'close']=100.0

def cycle(label,signal=None,pre=None):
 x=g['cycle'](200+len(g['TIMELINE']),force_signal=signal,pre=pre,label=label)
 check(label+' no harness exception',not x['harness_exc'],x['harness_exc'])
 check(label+' no swallowed engine error',not any('运行失败' in line or '执行信号失败' in line or 'UNHANDLED' in line for line in x['key_lines']),x['key_lines'])
 return x
for side,opposite in [('LONG','SHORT'),('SHORT','LONG')]:
 reset();a=cycle('seed '+side,side);check('seed actual '+side,SIM.pos[side]==1)
 SIM.rules=[g['rule']('reverse_close',g['ERR_INTERNAL'])]
 b=cycle('reverse close fails '+side,opposite)
 check('failed close preserves '+side,SIM.pos[side]==1 and SIM.pos[opposite]==0 and b['state']['strategy']['side']==side.lower())
 SIM.rules=[g['rule']('reverse_open',g['ERR_MARGIN'])]
 c=cycle('reverse open fails '+side,opposite)
 check('close ok open fail flat '+side,SIM.pos[side]==0 and SIM.pos[opposite]==0 and c['state']['strategy'] is None,c['state'])
 SIM.rules=[];d=cycle('retry open '+opposite,opposite);check('retry opened '+opposite,SIM.pos[opposite]==1)
 if period=='short':
  SIM.rules=[g['rule']('close_signal',g['ERR_INTERNAL'])];x=cycle('close signal fails '+opposite,'CLOSE_'+opposite)
  check('close signal retains '+opposite,SIM.pos[opposite]==1 and x['state']['strategy'] is not None)
  SIM.rules=[];x=cycle('close signal retry '+opposite,'CLOSE_'+opposite);check('close signal cleans '+opposite,SIM.pos[opposite]==0 and x['state']['strategy'] is None)
 reset();cycle('trailing seed '+side,side)
 SIM.rules=[g['rule']('trailing_close',g['ERR_INTERNAL'])]
 def arm(eng,df):
  g['arm_trailing'](True)(eng,df)
  # Suppress unrelated natural signals; real trailing close branch stays live.
  eng.detect_signal=lambda df:None
 x=cycle('trailing failure '+side,pre=arm)
 check('trailing failure preserves '+side,SIM.pos[side]==1 and x['state']['strategy'] is not None)
 SIM.rules=[];x=cycle('trailing retry '+side,pre=arm)
 check('trailing retry cleans '+side,SIM.pos[side]==0 and x['state']['strategy'] is None)
reset();cycle('model1 seed','LONG');g['set_model'](2)
x=cycle('model2 direct reverse','SHORT')
check('model2 must not auto-open via direct execute_signal',SIM.pos['SHORT']==0,x['requests'])
check('model2 reverse still closes LONG',SIM.pos['LONG']==0 and x['state']['strategy'] is None)
reset();cycle('model1 seed short','SHORT');g['set_model'](2)
x=cycle('model2 direct reverse long','LONG')
check('model2 must not auto-open LONG via direct execute_signal',SIM.pos['LONG']==0,x['requests'])
check('model2 reverse still closes SHORT',SIM.pos['SHORT']==0 and x['state']['strategy'] is None)
for side in ['LONG','SHORT']:
 reset();g['set_model'](2);x=cycle('model2 flat '+side,side)
 check('model2 flat no open '+side,not x['requests'] and SIM.pos[side]==0,x['requests'])
reset();g['set_model'](2)
# Real exchange parser and real claim manager; no fabricated success.
def manual(eng,df):
 SIM.pos['LONG']=1;SIM.entry['LONG']=100
 eng.claim_manager.infer_method='MARKET_PRICE'
x=cycle('model2 external claim',pre=manual)
check('model2 external claim creates strategy and trailing',x['state']['strategy'] is not None and x['state']['tsm']['has_position'],x['state'])
g['set_model'](1);x=cycle('model2 to model1 restart',pre=lambda eng,df:setattr(eng,'detect_signal',lambda df:None));check('restart state survives',x['state']['strategy'] is not None and x['state']['tsm']['has_position'],x['state'])
for model in [2,3]:
 reset();g['set_model'](model)
 def manual_short(eng,df):
  SIM.pos['SHORT']=1;SIM.entry['SHORT']=100
  eng.claim_manager.infer_method='MARKET_PRICE'
 x=cycle('model'+str(model)+' external SHORT',pre=manual_short)
 check('model'+str(model)+' claim SHORT exact state',x['state']['strategy']=={'side':'short','amount':1.0,'entry_price':100.0} and x['state']['tsm']['side']=='short' and x['state']['tsm']['has_position'],x['state'])
 x=cycle('model'+str(model)+' SHORT restart',pre=lambda eng,df:setattr(eng,'detect_signal',lambda df:None))
 check('model'+str(model)+' SHORT restart survives',x['state']['strategy'] is not None and x['state']['strategy']['side']=='short' and SIM.pos['SHORT']==1,x['state'])
reset()
for i in range(12):cycle('normal uninterrupted '+str(i))
Path(out).mkdir(parents=True,exist_ok=True)
Path(out,'oracles.json').write_text(json.dumps({'period':period,'checks':checks,'timeline':g['TIMELINE'],'rules':g['ALL_RULES']},indent=2,default=str))
print(json.dumps({'period':period,'checks':len(checks),'failed':[x for x in checks if not x['passed']]},indent=2))
sys.exit(1 if any(not x['passed'] for x in checks) else 0)
