#!/usr/bin/env python3
"""Disposable offline VM only: actual persistent filesystem capacity, not a mock quota."""
import errno,json,os,pathlib,subprocess,traceback
P=pathlib.Path
def emit(report):
    line='KANE_STORAGE_RESULT '+json.dumps(report)
    print(line,flush=True)
    # systemd's default stdout is the journal, unlike cloud-init's console.
    # Explicit serial evidence is required on the second boot as well.
    with open('/dev/ttyS0','w') as serial: serial.write(line+'\n')

def run(*args,**kw): return subprocess.run(args,check=True,text=True,capture_output=True,**kw)
def main():
    assert P('/etc/kane-storage-validation').read_text()=='disposable-vm\n'
    home=P('/mnt/kane-home');home.mkdir(exist_ok=True)
    second=P('/var/tmp/storage-first.json').exists()
    if not second:
        run('truncate','-s','64M','/var/tmp/home.ext4')
        run('mkfs.ext4','-q','-F','-m','0','/var/tmp/home.ext4')
    run('mount','-o','loop,nosuid,nodev,noexec','/var/tmp/home.ext4',str(home))
    os.chown(home,1000,1000);os.chmod(home,0o700)
    prefix=['setpriv','--reuid=1000','--regid=1000','--clear-groups','/usr/local/bin/kanesys','storage-check']
    check=json.loads(run(*prefix,'--path',str(home),'--max-bytes',str(64*1024*1024)).stdout)
    assert 0<check['capacity_bytes']<=64*1024*1024
    for path,limit in [(str(home),1024),(str(home.parent),2**63-1)]:
        result=subprocess.run([*prefix,'--path',path,'--max-bytes',str(limit)],capture_output=True)
        assert result.returncode!=0
    child=r'''
import os,errno,json,pathlib
h=pathlib.Path('/mnt/kane-home')
if not (h/'canary').exists(): (h/'canary').write_text('KANE_PERSISTENT_STORAGE')
assert (h/'canary').read_text()=='KANE_PERSISTENT_STORAGE'
n=0
try:
 with (h/'fill').open('ab',buffering=0) as f:
  while n<128*1024*1024:
   n+=f.write(b'x'*65536)
  os.fsync(f.fileno())
 raise AssertionError('capacity was not enforced')
except OSError as e:
 assert e.errno==errno.ENOSPC,e
 print(json.dumps({'errno':e.errno,'written_this_boot':n,'file_size':(h/'fill').stat().st_size,'canary':(h/'canary').read_text()}))
'''
    evidence=json.loads(run('setpriv','--reuid=1000','--regid=1000','--clear-groups','python3','-c',child).stdout)
    assert evidence['file_size']<=64*1024*1024
    if second: assert evidence['written_this_boot']==0
    report={'passed':True,'boot':2 if second else 1,'rust_check':check,'kernel_write':evidence,'os':P('/etc/os-release').read_text(),'kernel':run('uname','-r').stdout.strip()}
    if not second:
        P('/var/tmp/storage-first.json').write_text(json.dumps(report))
        P('/etc/systemd/system/kane-storage-verify.service').write_text('[Unit]\nAfter=local-fs.target\n[Service]\nType=oneshot\nExecStart=/usr/bin/python3 /usr/local/libexec/storage-test.py\nExecStartPost=/usr/bin/systemctl poweroff\n[Install]\nWantedBy=multi-user.target\n')
        run('systemctl','enable','kane-storage-verify.service')
    P('/var/tmp/storage-second.json' if second else '/var/tmp/storage-first.json').write_text(json.dumps(report))
    emit(report)
    run('umount',str(home))

try:main()
except Exception:
    traceback.print_exc()
    emit({'passed':False})
