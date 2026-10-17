from pathlib import Path
import shutil, ast, json, hashlib
import os
P=Path(__file__).resolve().parents[2]; E=Path(os.environ['BIT_EVIDENCE_ROOT']).resolve(); D=E/'isolated'
if E == P or P in E.parents:
 raise ValueError('Evidence must be outside the repository')
D.mkdir(parents=True,exist_ok=True)
manifest={}
# bit/B_TEST 不随公开仓库发布；缺失时跳过该目录而不是崩溃。
for directory in ['bit/strategy','bit/common','bit/utils','bit/B_TEST','common']:
 if not (P/directory).is_dir():
  print('skip: %s not present in this distribution' % directory)
  continue
 for f in (P/directory).rglob('*.py'):
  if f.is_symlink() or 'config' in f.name.lower(): continue
  t=D/f.relative_to(P); t.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(f,t); manifest[str(f.relative_to(P))]=hashlib.sha256(f.read_bytes()).hexdigest()
for name in ['bit/__init__.py','bit/config/feature_switches.py','bit/config/switch_model.py']:
 t=D/name;t.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(P/name,t)
(D/'bit/config/__init__.py').write_text('')
(D/'bit/config/config.py').write_text("# SYNTHETIC logging constants only\nLOG_LEVEL='INFO'\nLOG_FILE='offline.log'\n")
fixture="""# SYNTHETIC ONLY. No source configuration read.
TRADE_SYMBOL='MAVIA/USDT'
TRADE_AMOUNT=10
TIMEFRAME_LONG='1h'
TIMEFRAME_SHORT='15m'
DMR_LONG_PERIOD=6
DMR_SHORT_PERIOD=12
TRAILING_STOP_CONFIG={'enabled':True}
TIMEFRAME_MINUTES={'1h':60,'15m':15}
"""
(D/'bit/config/bit_config.py').write_text(fixture)
for period,tf,dmr in [('long','1h',6),('short','15m',12)]:
 cfg={'symbol':'MAVIA/USDT','timeframe':tf,'dmr_period':dmr,'position_size':10,'api_key':'DUMMY','api_secret':'DUMMY','strategy_id':'OFFLINE_'+period,'log_config':{'log_file':str(D/('logs/'+period+'.log'))},'data_config':{'rate_limit':False,'recv_window':10000,'data_limit':600},'signal_config':{'position_correction_enabled':True,'correction_threshold':0.0005,'correction_min_interval':0}}
 (D/f'bit/config/bit_{period}_term_config.py').write_text(fixture+f'\n{period.upper()}_TERM_CONFIG={cfg!r}\ndef get_{period}_term_config(): return {period.upper()}_TERM_CONFIG\n')
(D/'bit/data/bit_positions').mkdir(parents=True,exist_ok=True)
(D/'logs').mkdir(exist_ok=True)
(E/'manifest.json').write_text(json.dumps(manifest,indent=2))

for name in ['drive_bit_offline.py','oracles.py','no_network.py']:
 shutil.copy2(Path(__file__).parent/name,E/name)
(E/'guard').mkdir(exist_ok=True)
shutil.copy2(Path(__file__).parent/'guard/sitecustomize.py',E/'guard/sitecustomize.py')
print('Prepared allowlisted source snapshot:',len(manifest))
