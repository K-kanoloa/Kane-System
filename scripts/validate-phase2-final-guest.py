#!/usr/bin/env python3
"""Offline, disposable Linux only: real delegated cgroups and strict Kanaloa tests."""
import hashlib,json,os,subprocess,threading,time,traceback
from pathlib import Path
P=Path
ROOT=Path('/workspace/Kane-System');OUT=Path('/exchange')
def run(args,**kw):return subprocess.run(args,check=True,text=True,capture_output=True,**kw)
def main():
 run(['mount','-t','9p','-o','trans=virtio,version=9p2000.L','evidence',str(OUT)])
 Path('/mnt/payload').mkdir(exist_ok=True);run(['mount','-o','ro','/dev/sr0','/mnt/payload'])
 cached=Path('/etc/kane-pr4-cached-dependencies').is_file()
 for name in ([] if cached else ['payload.tar.gz'])+['source-overlay.tar.gz','extra-payload.tar.gz']:run(['tar','-xzf','/mnt/payload/'+name,'-C','/'])
 run(['umount','/mnt/payload'])
 (OUT/'cgroup-environment.json').write_text(json.dumps({'mountinfo':Path('/proc/self/mountinfo').read_text(),'self_cgroup':Path('/proc/self/cgroup').read_text(),'root_listing':[p.name for p in Path('/sys/fs/cgroup').iterdir()],'cloud_unit':run(['systemctl','cat','cloud-final.service']).stdout},indent=2))
 Path('/sys/fs/cgroup/cgroup.subtree_control').write_text('+cpu +memory +pids')
 delegated=Path('/sys/fs/cgroup/kane-p2-validation');delegated.mkdir();(delegated/'cgroup.subtree_control').write_text('+cpu +memory +pids')
 for p in [delegated,*delegated.iterdir()]:
  if p.name in ['kane-p2-validation','cgroup.procs','cgroup.threads','cgroup.subtree_control']:os.chown(p,1000,1000)
 control=delegated/'control';control.mkdir();models=delegated/'models';models.mkdir();(models/'cgroup.subtree_control').write_text('+cpu +memory +pids')
 for p in [control,models,*control.iterdir(),*models.iterdir()]:
  if p.is_dir() or p.name in ['cgroup.procs','cgroup.threads','cgroup.subtree_control']:os.chown(p,1000,1000)
 home_root=Path('/mnt/kane-p2-homes');home_root.mkdir(exist_ok=True)
 for i in range(11):
  h=home_root/str(i);h.mkdir();image=f'/var/tmp/kane-p2-home-{i}.ext4'
  run(['truncate','-s','64M',image]);run(['mkfs.ext4','-q','-F','-m','0',image]);run(['mount','-o','loop,nosuid,nodev,noexec',image,str(h)]);os.chown(h,1000,1000);os.chmod(h,0o700)
 daemon_log=(OUT/'dockerd.log').open('w');daemon=subprocess.Popen(['/usr/local/bin/dockerd','--host=unix:///var/run/docker.sock','--storage-driver=vfs','--iptables=false','--ip6tables=false','--bridge=none','--ip-forward=false','--ip-masq=false'],stdout=daemon_log,stderr=subprocess.STDOUT)
 try:
  for _ in range(300):
   if subprocess.run(['docker','info'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:break
   if daemon.poll() is not None:raise RuntimeError('guest dockerd exited')
   time.sleep(.3)
  else:raise RuntimeError('guest dockerd timeout')
  if not cached:run(['docker','load','-i','/var/tmp/kane-reference-image.tar'],timeout=120)
  os.chown('/var/run/docker.sock',0,1000);os.chmod('/var/run/docker.sock',0o660)
  evidence=Path('/var/tmp/kane-p2f-evidence');evidence.mkdir(exist_ok=True);os.chown(evidence,1000,1000)
  runtime=Path('/var/tmp/kane-p2f-runtime');runtime.mkdir(exist_ok=True);os.chown(runtime,1000,1000);os.chmod(runtime,0o700)
  env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':'/home/ubuntu','PYTHONPATH':f'{ROOT}/apps/api:{ROOT}/apps/api/tests:/opt/kane-test-python','KANE_PR1_TEST':'1','KANE_PR2_TEST':'1','KANE_PHASE2_TEST':'1','KANE_PHASE2_FINAL_TEST':'1','KANE_PHASE2_KERNEL_TEST':'1','KANE_LOCAL_MODEL_SERVER':'/opt/kane-local-model/llama-server','KANE_LOCAL_MODEL_FILE':'/opt/kane-local-model/SmolLM2-135M-Instruct-Q4_K_M.gguf','KANE_PHASE2_CGROUP_ROOT':str(models),'KANE_PHASE2_HOME_ROOT':str(home_root),'KANE_TEST_RUNTIME_ROOT':str(runtime)}
  launch="import os,sys;from pathlib import Path;Path('/sys/fs/cgroup/kane-p2-validation/control/cgroup.procs').write_text(str(os.getpid()));os.setgroups([]);os.setgid(1000);os.setuid(1000);os.execve('/usr/bin/python3',sys.argv[1:],os.environ)"
  stop_observer=threading.Event()
  def observe():
   last_logs=0
   while not stop_observer.wait(5):
    data={'time':time.time(),'meminfo':Path('/proc/meminfo').read_text(),'processes':run(['ps','-eo','pid,ppid,pcpu,cputime,rss,comm']).stdout,'runtime_logs':{str(p):p.read_text()[-4000:] for p in runtime.rglob('runtime.log')},'models':{}}
    for g in models.glob('model-*'):
     try:data['models'][g.name]={n:(g/n).read_text() for n in ['cpu.max','cpu.stat','memory.max','memory.current','memory.events','pids.max','pids.current','pids.events']}
     except OSError:pass
    data['container_logs']={}
    try:
     if time.monotonic()-last_logs<60:
      (OUT/'observer-current.json').write_text(json.dumps(data,indent=2));continue
     last_logs=time.monotonic()
     ids=run(['docker','ps','--filter','label=kane.system.agent','--format','{{.ID}}']).stdout.split()
     for ident in ids:
      log=subprocess.run(['docker','logs','--tail','80',ident],text=True,capture_output=True,timeout=10)
      data['container_logs'][ident]=log.stdout+log.stderr
    except Exception:pass
    (OUT/'observer-current.json').write_text(json.dumps(data,indent=2))
  observer=threading.Thread(target=observe,daemon=True);observer.start()
  cases=[]
  tests=['test_actual_model_kernel_cpu_pids_and_oom','test_ten_kanaloa_strict_resources[False-False]','test_ten_kanaloa_strict_resources[True-False]','test_ten_kanaloa_strict_resources[True-True]']
  for index,name in enumerate(tests):
   # Fresh filesystems for each scenario, never touch Owner storage.
   if index:
    for i in range(11):
     h=home_root/str(i);run(['umount',str(h)]);run(['mkfs.ext4','-q','-F','-m','0',f'/var/tmp/kane-p2-home-{i}.ext4']);run(['mount','-o','loop,nosuid,nodev,noexec',f'/var/tmp/kane-p2-home-{i}.ext4',str(h)]);os.chown(h,1000,1000);os.chmod(h,0o700)
   target='apps/api/tests/test_phase2_kernel.py::'+name
   with (OUT/f'test-{index}.log').open('w') as log:
    code=subprocess.run(['/usr/bin/python3','-c',launch,'python3','-m','pytest','-q','-rs','-s','-p','no:cacheprovider',target,'--basetemp',str(evidence/str(index))],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=2400).returncode
   run(['tar','-czf',str(OUT/f'case-{index}-homes.tar.gz'),'-C',str(home_root),'.'],timeout=120)
   trim=run(['fstrim','-v','/'])
   (OUT/f'case-{index}-trim.log').write_text(trim.stdout)
   cases.append({'test':name,'exit_code':code});(OUT/'progress.json').write_text(json.dumps(cases,indent=2))
   if code:raise RuntimeError(f'{name} failed: {code}')
  (OUT/'guest-result.json').write_text(json.dumps({'passed':True,'scope':'Real Kanaloa, model cgroups v2, strict host pool and ext4; real/Mock separate','cases':cases,'kernel':run(['uname','-r']).stdout.strip(),'os':Path('/etc/os-release').read_text()},indent=2))
 finally:
  # Persist logs, ledgers, receipts and fixture files even on an initial failure.
  members=['var/tmp/kane-p2f-evidence','var/tmp/kane-p2f-runtime']
  for p in home_root.iterdir():members.append(str(p).lstrip('/'))
  run(['tar','-czf',str(OUT/'evidence.tar.gz'),'-C','/',*members],timeout=120)
  stop_observer.set();observer.join(timeout=10)
  daemon.terminate();daemon.wait(timeout=40);daemon_log.close()
def run_guest():
 marker=P('/etc/kane-storage-runtime-validation')
 if os.geteuid()!=0 or not marker.is_file() or marker.read_text()!='disposable-vm\n':raise SystemExit('disposable root VM required')
 OUT.mkdir(exist_ok=True)
 try:main()
 except BaseException:
  text=traceback.format_exc();print(text,flush=True)
  if OUT.is_mount():(OUT/'guest-error.txt').write_text(text)
 finally:subprocess.run(['systemctl','poweroff'],check=False)
if __name__=='__main__':run_guest()
