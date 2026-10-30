#!/usr/bin/env python3
"""Two-boot offline PR-4 acceptance. Refuses to run outside the marked VM."""
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import traceback

ROOT = Path('/workspace/Kane-System')
OUT = Path('/exchange')
CONTROL = Path('/var/tmp/kane-pr4-control')
P = Path

def run(args, **kw):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kw)

def main():
    OUT.mkdir(exist_ok=True)
    run(['mount','-t','9p','-o','trans=virtio,version=9p2000.L','evidence',str(OUT)])
    phase = 2 if Path('/var/tmp/kane-pr4-first-boot-complete').exists() else 1
    selection=Path('/etc/kane-pr4-test-set')
    normal_stop=selection.is_file() and selection.read_text()=='normal-stop\n'
    phase1=selection.is_file() and selection.read_text()=='phase1\n'
    cached=Path('/etc/kane-pr4-cached-dependencies').is_file()
    if phase == 1:
        Path('/mnt/payload').mkdir(exist_ok=True)
        run(['mount','-o','ro','/dev/sr0','/mnt/payload'])
        if cached:
            assert (normal_stop or phase1) and Path('/opt/kane-test-python/pytest').is_dir()
            assert Path('/usr/local/bin/node').is_file()
        for archive in (('source-overlay.tar.gz',) if cached else ('payload.tar.gz','source-overlay.tar.gz')):
            run(['tar','-xzf','/mnt/payload/'+archive,'-C','/'])
        run(['umount','/mnt/payload'])
    CONTROL.mkdir(exist_ok=True)
    os.chown(CONTROL,1000,1000)
    os.chmod(CONTROL,0o700)
    daemon = None
    logs = []
    daemon_events = []
    def start_daemon():
        nonlocal daemon
        log = (OUT/f'boot-{phase}-dockerd-{len(logs)}.log').open('w')
        logs.append(log)
        daemon = subprocess.Popen(['/usr/local/bin/dockerd','--host=unix:///var/run/docker.sock',
            '--storage-driver=vfs','--iptables=false','--ip6tables=false','--bridge=none',
            '--ip-forward=false','--ip-masq=false'],stdout=log,stderr=subprocess.STDOUT)
        deadline=time.monotonic()+90
        while subprocess.run(['docker','info'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
            if daemon.poll() is not None or time.monotonic()>deadline: raise RuntimeError('dockerd did not start')
            time.sleep(.3)
        os.chown('/var/run/docker.sock',0,1000);os.chmod('/var/run/docker.sock',0o660)
        daemon_events.append({'event':'started','pid':daemon.pid,'time':time.time()})
    def stop_daemon():
        if daemon and daemon.poll() is None:
            daemon.send_signal(signal.SIGCONT)
            daemon.terminate()
            try: daemon.wait(timeout=30)
            except subprocess.TimeoutExpired: daemon.kill();daemon.wait(timeout=10)
            daemon_events.append({'event':'stopped','pid':daemon.pid,'time':time.time()})
    start_daemon()
    if phase==1 and not cached: run(['docker','load','-i','/var/tmp/kane-reference-image.tar'],timeout=120)
    for i in range(5):
        home=Path(f'/mnt/kane-pr4-home-{i}');home.mkdir(exist_ok=True)
        image=f'/var/tmp/kane-pr4-home-{i}.ext4'
        if phase==1:
            run(['truncate','-s','64M',image]);run(['mkfs.ext4','-q','-F','-m','0',image])
        run(['mount','-o','loop,nosuid,nodev,noexec',image,str(home)])
        os.chown(home,1000,1000);os.chmod(home,0o700)
    stop=threading.Event()
    def controller():
        # Only fixed daemon actions in this disposable VM. Never arbitrary
        # commands, production sockets, ports or user-supplied PIDs.
        while not stop.wait(.1):
            request=CONTROL/'request.json'
            if not request.exists(): continue
            data=json.loads(request.read_text());request.unlink()
            try:
                if data['action']=='freeze':
                    daemon.send_signal(signal.SIGSTOP)
                    daemon_events.append({'event':'frozen','pid':daemon.pid,'time':time.time()})
                elif data['action']=='stop': stop_daemon()
                elif data['action']=='start': start_daemon()
                else: raise ValueError('fixed action required')
                response={'ok':True,'action':data['action'],'pid':daemon.pid}
            except Exception as exc: response={'ok':False,'error':str(exc)}
            temp=CONTROL/'reply.tmp';temp.write_text(json.dumps(response));os.chown(temp,1000,1000)
            temp.replace(CONTROL/'reply.json')
    thread=threading.Thread(target=controller,daemon=True);thread.start()
    evidence=Path('/var/tmp/kane-pr4-evidence');evidence.mkdir(exist_ok=True);os.chown(evidence,1000,1000)
    runtime_root=Path('/var/tmp/kane-pr4-runtime');runtime_root.mkdir(exist_ok=True)
    os.chown(runtime_root,1000,1000);os.chmod(runtime_root,0o700)
    env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':'/home/ubuntu',
         'PYTHONPATH':f'{ROOT}/apps/api:/opt/kane-test-python','KANE_PR1_TEST':'1','KANE_PR2_TEST':'1',
         'KANE_PR4_TEST':'1','KANE_PR4_CLOSE_TEST':'1','KANE_TEST_RUNTIME_ROOT':str(runtime_root)}
    tests= ['test_pool_and_real_daemon_restart','test_prepare_whole_host_reboot'] if phase==1 else ['test_after_whole_host_reboot']
    if normal_stop: tests=['test_normal_stop_releases_runtime_resources']
    if phase1:
        env['KANE_STORAGE_TEST']='1';env['KANE_STORAGE_HOME']='/mnt/kane-home'
        tests=['test_pool_and_real_daemon_restart','test_normal_stop_releases_runtime_resources',
               'storage:test_bounded_home_persists_original_session_without_replay',
               'storage:test_enospc_keeps_task_uncertain_and_does_not_replay']

    cases=[]
    try:
        for name in tests:
            target=f'apps/api/tests/test_pr4_close.py::{name}'
            if name.startswith('storage:'):
                # Fresh dedicated ext4 image for EACH capacity fault; never
                # resize or reformat a developer/Owner filesystem.
                home=Path('/mnt/kane-home');home.mkdir(exist_ok=True)
                image='/var/tmp/kane-phase1-home.ext4'
                run(['truncate','-s','64M',image]);run(['mkfs.ext4','-q','-F','-m','0',image])
                run(['mount','-o','loop,nosuid,nodev,noexec',image,str(home)])
                os.chown(home,1000,1000);os.chmod(home,0o700)
                target='apps/api/tests/test_storage_runtime.py::'+name.split(':',1)[1]
            with (OUT/f'{name}.log').open('w') as log:
                code=subprocess.run(['setpriv','--reuid=1000','--regid=1000','--clear-groups',
                    'python3','-m','pytest','-q','-rs','-s','-p','no:cacheprovider',
                    target,'--basetemp',str(evidence/name)],
                    cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=720).returncode
            cases.append({'test':name,'exit_code':code})
            (OUT/f'boot-{phase}-progress.json').write_text(json.dumps(cases,indent=2))
            if code!=0: raise RuntimeError(f'{name}: exit {code}')
            if name.startswith('storage:'):
                run(['tar','-czf',str(OUT/(name.split(':',1)[1]+'-home.tar.gz')),'--exclude=capacity-fill','-C','/mnt/kane-home','.'],timeout=90)
                run(['umount','/mnt/kane-home'])
        if normal_stop or phase1:
            (OUT/'guest-result.json').write_text(json.dumps({'passed':True,'scope':'normal Supervisor exit before Core shutdown; strict reference profile',
                'cases':cases,'os':Path('/etc/os-release').read_text(),'kernel':run(['uname','-r']).stdout.strip()},indent=2))
        elif phase==1:
            assert (CONTROL/'reboot.json').is_file(), 'reboot preparation evidence missing'
            Path('/var/tmp/kane-pr4-first-boot-complete').write_text('prepared\n')
            os.sync() # Whole VM shutdown below; next QEMU start boots this disk.
        else:
            first=json.loads((OUT/'boot-1-progress.json').read_text())
            report={'passed':True,'scope':'Ubuntu x86_64 Docker vfs ext4 + original Kanaloa + deterministic mock',
                    'boots':2,'cases':first+cases,'os':Path('/etc/os-release').read_text(),
                    'kernel':run(['uname','-r']).stdout.strip()}
            (OUT/'guest-result.json').write_text(json.dumps(report,indent=2))
    finally:
        stop.set();thread.join(timeout=100)
        (OUT/f'boot-{phase}-daemon-events.json').write_text(json.dumps(daemon_events,indent=2))
        # Preserve all native sessions, databases, logs and pytest evidence.
        members=['var/tmp/kane-pr4-evidence','var/tmp/kane-pr4-control','var/tmp/kane-pr4-runtime']
        members += [str(p).lstrip('/') for p in Path('/tmp').glob('ks-pr2-*')]
        members += [f'mnt/kane-pr4-home-{i}' for i in range(5)]
        run(['tar','-czf',str(OUT/f'boot-{phase}-evidence.tar.gz'),'-C','/',*members],timeout=90)
        # Do not gracefully stop the daemon on boot 1: the VM shutdown must
        # exercise loss of the live Supervisor/Core/Agent, not fixture cleanup.
        if phase==2 or normal_stop or phase1: stop_daemon()
        for log in logs: log.close()

def run_guest():
    marker=P('/etc/kane-storage-runtime-validation')
    if os.geteuid()!=0 or not marker.is_file() or marker.read_text()!='disposable-vm\n':
        raise SystemExit('Refusing: disposable root VM marker required')
    try: main()
    except Exception:
        error=traceback.format_exc();print(error,flush=True)
        if OUT.is_mount(): (OUT/'guest-error.txt').write_text(error)
    finally: subprocess.run(['systemctl','poweroff'],check=False)

if __name__=='__main__': run_guest()
