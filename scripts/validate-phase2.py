#!/usr/bin/env python3
"""Validate an immutable Phase 2 source tree using explicitly pinned local caches.

No cloud credentials, source commits, publishing, Host upgrade or Phase closure.
Run from an isolated Git tree checkout with cached Node packages and API Python.
"""
import argparse,hashlib,importlib.metadata,json,os,shutil,subprocess,sys,tempfile,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def main(phase=2):
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--output',required=True,type=Path);p.add_argument('--source-tree',required=True)
 p.add_argument('--final-gaps',action='store_true',help='Include final real-tool tests; kernel scenarios run separately in the offline VM')
 args=p.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
 env=os.environ.copy();env.update(CARGO_INCREMENTAL='0',CARGO_PROFILE_DEV_DEBUG='0',CARGO_PROFILE_TEST_DEBUG='0',PYTHONPATH=str(ROOT/'apps/api')+':'+str(ROOT/'apps/api/tests'))
 for name in ['KANE_PR1_TEST','KANE_PR2_TEST','KANE_PR3A_TEST','KANE_PR3B_TEST','KANE_PR4_TEST','KANE_FIFO_TEST','KANE_PHASE1_TEST','KANE_PHASE2_TEST']:env[name]='1'
 env.pop('KANE_PHASE0_TEST',None)
 if phase==3:env['KANE_PHASE3_TEST']='1'
 if args.final_gaps:env['KANE_PHASE2_FINAL_TEST']='1'
 for name in ['DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH']:env.pop(name,None)
 env['DOCKER_HOST']='unix:///var/run/docker.sock'
 for name in ['KANE_LOCAL_MODEL_SERVER','KANE_LOCAL_MODEL_FILE']:
  assert name in env and Path(env[name]).is_file(),f'pinned local dependency required: {name}'
 target=Path(env.get('CARGO_TARGET_DIR',str(ROOT/'target'))).resolve()
 if target!=ROOT/'target':
  # Git's /target/ rule ignores a directory, not a top-level symlink. Keep a
  # real ignored directory and link its generated debug output explicitly.
  local=ROOT/'target';assert not local.is_symlink(),'use an isolated real target directory'
  local.mkdir(exist_ok=True)
  if (local/'debug').exists():assert (local/'debug').resolve()==target/'debug','stale binary output would invalidate source acceptance'
  else:(local/'debug').symlink_to(target/'debug',target_is_directory=True)
 commands=[]
 def run(label,command):
  start=time.monotonic()
  with (out/(label+'.log')).open('wb') as log:
   code=subprocess.run(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT).returncode
  commands.append({'check':label,'command':command,'exit_code':code,'seconds':time.monotonic()-start})
  (out/'commands.json').write_text(json.dumps(commands,indent=2));print(json.dumps(commands[-1]),flush=True)
  return code==0
 assert not subprocess.run(['git','diff','--quiet',args.source_tree],cwd=ROOT).returncode
 assert not subprocess.check_output(['git','ls-files','--others','--exclude-standard'],cwd=ROOT)
 profile={'date_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'tested_source_tree':args.source_tree,'source_root':str(ROOT),'python':sys.version,'dependencies':{k:importlib.metadata.version(k) for k in ['pytest','pytest-asyncio','httpx','fastapi']},'build':{'locked':True,'offline':True,'incremental':False,'external_target_cache':str(target),'fresh_dependency_install':False},'models':{'mock':'existing kane_models::DeterministicMock through OpenAI wire fixture','real':'pinned public local CPU GGUF; no real invoice'},'local_model_dependencies':{name:{'path':env[name],'sha256':hashlib.sha256(Path(env[name]).read_bytes()).hexdigest()} for name in ['KANE_LOCAL_MODEL_SERVER','KANE_LOCAL_MODEL_FILE']},'scope':'Local Linux x86-64, actual DSH/Docker and cgroups reference, restricted tools; not overall OS delivery'}
 (out/'profile.json').write_text(json.dumps(profile,indent=2))
 okay=run('source-clean-before',['git','diff','--exit-code',args.source_tree])
 okay=run('fmt',['cargo','fmt','--all','--check']) and okay
 built=run('build',['cargo','build','--workspace','--locked','--offline']);okay=built and okay
 if built:
  okay=run('rust',['cargo','test','--workspace','--locked','--offline']) and okay
  okay=run('clippy',['cargo','clippy','--workspace','--all-targets','--locked','--offline','--','-D','warnings']) and okay
  okay=run('environment',[sys.executable,str(ROOT/'bench/phase2/probe.py')]) and okay
  okay=run('rust-version',['rustc','--version']) and okay
  okay=run('node-version',['node','--version']) and okay
  okay=run('node-transport-version',['node','-e',"console.log(require('undici/package.json').version)"]) and okay
  okay=run('rust-dependencies',['cargo','tree','--workspace','--locked','--offline']) and okay
  okay=run('python-source-path',[sys.executable,'-c','import app.system.client; print(app.system.client.__file__)']) and okay
  temp=Path(tempfile.mkdtemp(prefix='p2-final-'))
  okay=run('python-regression',[sys.executable,'-m','pytest','-q','-rs','apps/api/tests','--basetemp',str(temp/'cases'),'--junitxml',str(out/'pytest.xml')]) and okay
  for file in (temp/'cases').rglob('*'):
   if file.is_file() and not file.is_symlink() and file.suffix in ['.json','.log','.txt','.jsonl']:
    dest=out/'cases'/file.relative_to(temp/'cases');dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(file,dest)
  (out/'case-origin.json').write_text(json.dumps({'directory':str(temp/'cases'),'raw_originals_retained':True,'exported_extensions':['.json','.log','.txt','.jsonl'],'sqlite_and_sockets_not_exported':True},indent=2))
  (out/'binary-sha256.txt').write_text(hashlib.sha256((ROOT/'target/debug/kanesys').read_bytes()).hexdigest()+'\n')
 okay=run('source-clean-after',['git','diff','--exit-code',args.source_tree]) and okay
 assert not subprocess.check_output(['git','ls-files','--others','--exclude-standard'],cwd=ROOT)
 (out/'result.json').write_text(json.dumps({'passed':okay,'source_tree':args.source_tree,f'phase{phase}_closed':False,'commits_created':0,'push':False,'publish':False,'runtime_tests_rerun':True,'unmanaged_host_ten_deselected':0,'strict_kernel_ten':'Separate offline VM; see kernel acceptance evidence' if args.final_gaps else None},indent=2))
 return 0 if okay else 1
if __name__=='__main__':raise SystemExit(main())
