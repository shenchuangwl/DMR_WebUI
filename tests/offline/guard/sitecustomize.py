import sys, os
from pathlib import Path
ROOT=os.path.realpath(os.environ['BIT_PRODUCTION_ROOT'])+'/'
SAFE=os.path.realpath(os.environ['BIT_EVIDENCE_ROOT'])+'/'
VENV=os.path.realpath(os.environ['BIT_VENV_ROOT'])+'/'
def audit(event,args):
    if event == 'open':
        path,mode,flags=args
        if isinstance(path,(str,bytes)):
            path=os.path.realpath(os.fsdecode(path))
            writing=(isinstance(mode,str) and any(x in mode for x in 'wax+')) or (isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC))
            if path.startswith(ROOT) and not (path.startswith(VENV) and not writing):
                raise PermissionError('OFFLINE: production path denied')
            if writing and not path.startswith(SAFE) and path!='/dev/null':
                raise PermissionError('OFFLINE: write outside sandbox denied: '+path)
    elif event in ('os.remove','os.rmdir','os.mkdir','os.rename'):
        for path in args[:2] if event=='os.rename' else args[:1]:
            if isinstance(path,(str,bytes)) and not os.path.realpath(os.fsdecode(path)).startswith(SAFE):
                raise PermissionError('OFFLINE: mutation outside sandbox denied')
sys.addaudithook(audit)
