#!/usr/bin/env python3
"""Reuse locked Phase2/3 regressions, add local Linux Phase4 control/browser tests.

No device emulation counts as platform acceptance. Never changes stage closure.
"""
import importlib.util,json,os,shutil,socket,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    for name in ['KANE_PREVIOUS_CORE_BINARY','KANE_PHASE3_SCHEMA7_BINARY']:
        if not os.getenv(name) or not Path(os.environ[name]).is_file():
            raise SystemExit(f'Pinned previous reader required before validation: {name}')
    for port in [9443,3144]:
        with socket.socket() as probe:
            try:probe.bind(('127.0.0.1',port))
            except OSError as error:raise SystemExit(f'Exclusive browser test port {port} unavailable; keep existing service intact: {error}')
    output=Path(sys.argv[sys.argv.index('--output')+1]).resolve()
    pre=output.with_name(output.name+'-web');pre.mkdir(parents=True,exist_ok=False)
    os.environ.update(KANE_PHASE3_TEST='1',KANE_PHASE4_TEST='1',NEXT_TELEMETRY_DISABLED='1')
    checks=[]
    for name,command in [('typecheck',['npm','run','typecheck:web']),('build',['npm','run','build:web']),('control-lint',['npx','--no-install','eslint','src/app/control'])]:
        start=time.monotonic()
        with (pre/(name+'.log')).open('w') as log:result=subprocess.run(command,cwd=ROOT/'apps/web' if name=='control-lint' else ROOT,stdout=log,stderr=subprocess.STDOUT)
        checks.append({'check':name,'command':command,'exit_code':result.returncode,'seconds':time.monotonic()-start})
    (pre/'commands.json').write_text(json.dumps(checks,indent=2))
    if any(x['exit_code'] for x in checks):return 1
    spec=importlib.util.spec_from_file_location('reused_validation',ROOT/'scripts/validate-phase2.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    result=module.main(phase=5 if os.getenv('KANE_PHASE5_TEST')=='1' else 4)
    shutil.copytree(pre,output/'web')
    origin=json.loads((output/'case-origin.json').read_text())
    for image in Path(origin['directory']).rglob('*.png'):
        destination=output/'cases'/image.relative_to(origin['directory']);destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(image,destination)
    return result
if __name__=='__main__':raise SystemExit(main())
