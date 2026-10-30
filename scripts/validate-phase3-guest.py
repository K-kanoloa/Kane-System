#!/usr/bin/env python3
"""Marked disposable two-boot Linux VM only; first boot is externally power-cut."""
import json,os,subprocess,time,traceback
from pathlib import Path
P=Path
ROOT=Path('/workspace/Kane-System');OUT=Path('/exchange');CONTROL=Path('/var/tmp/kane-phase3-control')
def run(args,**kw):return subprocess.run(args,check=True,text=True,capture_output=True,**kw)
def main():
    assert os.geteuid()==0 and Path('/etc/kane-storage-runtime-validation').read_text()=='disposable-vm\n'
    OUT.mkdir(exist_ok=True);run(['mount','-t','9p','-o','trans=virtio,version=9p2000.L','evidence',str(OUT)])
    phase=2 if (CONTROL/'before.json').is_file() else 1
    if phase==1:
        assert Path('/etc/kane-pr4-cached-dependencies').is_file(),'verified immutable cached VM is required by this profile'
        Path('/mnt/payload').mkdir(exist_ok=True);run(['mount','-o','ro','/dev/sr0','/mnt/payload'])
        for name in ['source-overlay.tar.gz','extra-payload.tar.gz']:run(['tar','-xzf','/mnt/payload/'+name,'-C','/'])
        run(['umount','/mnt/payload'])
    runtime=Path('/var/tmp/kane-phase3-runtime');runtime.mkdir(exist_ok=True);os.chown(runtime,1000,1000);os.chmod(runtime,0o700)
    evidence=Path('/var/tmp/kane-phase3-evidence');evidence.mkdir(exist_ok=True);os.chown(evidence,1000,1000)
    CONTROL.mkdir(exist_ok=True);os.chown(CONTROL,1000,1000);os.chmod(CONTROL,0o700)
    Path('/sys/fs/cgroup/cgroup.subtree_control').write_text('+cpu +memory +pids')
    delegated=Path('/sys/fs/cgroup/kane-phase3-validation');delegated.mkdir(exist_ok=True);(delegated/'cgroup.subtree_control').write_text('+cpu +memory +pids')
    control=delegated/'control';models=delegated/'models';control.mkdir(exist_ok=True);models.mkdir(exist_ok=True);(models/'cgroup.subtree_control').write_text('+cpu +memory +pids')
    for parent in [delegated,control,models]:
        for p in [parent,*parent.iterdir()]:
            if p.is_dir() or p.name in ['cgroup.procs','cgroup.threads','cgroup.subtree_control']:os.chown(p,1000,1000)
    for name in ['home','inflight','full']:
        path=Path('/mnt/kane-p3-'+name);path.mkdir(exist_ok=True);image='/var/tmp/kane-p3-'+name+'.ext4'
        if phase==1:run(['truncate','-s','64M',image]);run(['mkfs.ext4','-q','-F','-m','0',image])
        run(['mount','-o','loop,nosuid,nodev,noexec',image,str(path)]);os.chown(path,1000,1000);os.chmod(path,0o700)
    log=(OUT/f'boot-{phase}-dockerd.log').open('w');daemon=subprocess.Popen(['/usr/local/bin/dockerd','--host=unix:///var/run/docker.sock','--storage-driver=vfs','--iptables=false','--ip6tables=false','--bridge=none','--ip-forward=false','--ip-masq=false'],stdout=log,stderr=subprocess.STDOUT)
    for _ in range(300):
        if subprocess.run(['docker','info'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:break
        if daemon.poll() is not None:raise RuntimeError('private guest daemon exited')
        time.sleep(.3)
    else:raise RuntimeError('private daemon unavailable')
    os.chown('/var/run/docker.sock',0,1000);os.chmod('/var/run/docker.sock',0o660)
    env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':'/home/ubuntu','PYTHONPATH':f'{ROOT}/apps/api:{ROOT}/apps/api/tests:/opt/kane-test-python','KANE_PR1_TEST':'1','KANE_PR2_TEST':'1','KANE_PHASE3_TEST':'1','KANE_PHASE3_BOOT_TEST':'1','KANE_TEST_RUNTIME_ROOT':str(runtime),'KANE_LOCAL_MODEL_SERVER':'/opt/kane-local-model/llama-server','KANE_LOCAL_MODEL_FILE':'/opt/kane-local-model/SmolLM2-135M-Instruct-Q4_K_M.gguf'}
    (OUT/f'boot-{phase}-environment.json').write_text(json.dumps({'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'kernel':run(['uname','-r']).stdout.strip(),'os':Path('/etc/os-release').read_text(),'cgroup_mount':run(['stat','-fc','%T','/sys/fs/cgroup']).stdout.strip(),'model_delegation':str(models),'network':'QEMU -nic none / Agent network none','agent':'original Kanaloa/DSH under original Rust sandbox; no privileged Agent','core_sha256':run(['sha256sum',str(ROOT/'target/debug/kanesys')]).stdout.split()[0]},indent=2))
    launch="import os,sys;from pathlib import Path;Path('/sys/fs/cgroup/kane-phase3-validation/control/cgroup.procs').write_text(str(os.getpid()));os.setgroups([]);os.setgid(1000);os.setuid(1000);os.execve('/usr/bin/python3',sys.argv[1:],os.environ)"
    tests=['test_prepare_live_power_cut'] if phase==1 else ['test_recover_after_abrupt_whole_host_loss','test_checkpoint_enospc_never_publishes_a_checkpoint']
    cases=[]
    try:
        for name in tests:
            with (OUT/f'boot-{phase}-{name}.log').open('w') as output:
                code=subprocess.run(['/usr/bin/python3','-c',launch,'python3','-m','pytest','-q','-rs','-s','-p','no:cacheprovider','apps/api/tests/test_phase3_boot.py::'+name,'--basetemp',str(evidence/name)],cwd=ROOT,env=env,stdout=output,stderr=subprocess.STDOUT,timeout=1200).returncode
            cases.append({'test':name,'exit_code':code});(OUT/f'boot-{phase}-progress.json').write_text(json.dumps(cases,indent=2))
            if code:raise RuntimeError(f'{name} failed: {code}')
        assert phase==2,'first boot must be interrupted externally, never declared PASS'
        assert json.loads((OUT/'power-cut.json').read_text())['injected'] is True
        (OUT/'guest-result.json').write_text(json.dumps({'passed':True,'scope':'Actual Linux two-boot abrupt power cut; original Kanaloa context/Task outcome; actual CPU ModelInstance recovery and ext4 ENOSPC','phase1':'intentionally interrupted with live components; not a passed pytest case','phase2_cases':cases,'boot_before':json.loads((CONTROL/'before.json').read_text())['boot_id'],'boot_after':Path('/proc/sys/kernel/random/boot_id').read_text().strip()},indent=2))
    finally:
        # Second boot exports the first boot's durable and recovered raw state too.
        members=['var/tmp/kane-phase3-control','var/tmp/kane-phase3-runtime','var/tmp/kane-phase3-evidence','mnt/kane-p3-home','mnt/kane-p3-inflight']
        run(['tar','-czf',str(OUT/f'boot-{phase}-evidence.tar.gz'),'--exclude=phase3-capacity-fill','-C','/',*members],timeout=180)
        if phase==2:daemon.terminate();daemon.wait(timeout=60)
        log.close()
def run_guest():
    marker=P('/etc/kane-storage-runtime-validation')
    if os.geteuid()!=0 or not marker.is_file() or marker.read_text()!='disposable-vm\n':
        raise SystemExit('Refusing: explicit disposable root VM marker required')
    try:main()
    except Exception:
        error=traceback.format_exc();print(error,flush=True)
        if OUT.is_mount():(OUT/'guest-error.txt').write_text(error)
    finally:subprocess.run(['systemctl','poweroff'],check=False)

if __name__=='__main__':run_guest()
