#!/usr/bin/python3
"""Local physical console setup; no network credentials and no Agent execution."""
import getpass,json,os,re,subprocess,sys,time
from pathlib import Path

def run(*args,**kw):
    return subprocess.run(args,check=True,env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LC_ALL':'C.UTF-8'},**kw)
def rpc(request):
    p=run('/usr/bin/kanesys','--config','/etc/kane-system/core.env','host-request',input=json.dumps(request),text=True,capture_output=True)
    return json.loads(p.stdout)
def initialize():
    marker=Path('/etc/kane-system/owner-initialized.json')
    if marker.exists():
        print('Owner already initialized. Log in on the local console.');return
    print('Kane System — local Owner initialization. No cloud account is required.',flush=True)
    name=input('New local Owner username: ').strip()
    if not re.fullmatch('[a-z][a-z0-9_]{2,23}',name) or name in ['root','ubuntu','nobody','kane_system']:
        raise ValueError('Use 3–24 lowercase letters/digits/underscores, starting with a letter')
    password=getpass.getpass('Owner password (12+ characters): ')
    if len(password)<12 or any(c in password for c in '\n\r:\0') or password!=getpass.getpass('Confirm Owner password: '):
        raise ValueError('Password too short or confirmation mismatch')
    if subprocess.run(['getent','passwd',name],stdout=subprocess.DEVNULL).returncode:
        run('useradd','-m','-s','/bin/bash','-G','sudo',name)
    else:
        raise RuntimeError('Account exists; refuse to replace credentials')
    run('chpasswd',input=f'{name}:{password}\n',text=True);password=None
    # No automatic Task dispatch on first boot or later reboot.
    run('systemctl','start','kane-system.target')
    for _ in range(100):
        try:
            if rpc({'op':'health'}):break
        except subprocess.CalledProcessError:pass
        time.sleep(.1)
    else:raise RuntimeError('Core did not become ready; Owner setup remains incomplete')
    private=Path('/etc/kane-system/owner');private.mkdir(mode=0o700,exist_ok=True)
    grant=rpc({'op':'control_auth','action':'init','body':{'origin':'https://localhost:9443'}})
    out=private/'first-pairing.json'
    with out.open('x') as f:os.fchmod(f.fileno(),0o600);json.dump(grant,f);f.flush();os.fsync(f.fileno())
    with marker.open('x') as f:
        os.fchmod(f.fileno(),0o600);json.dump({'local_owner':name,'origin':'https://localhost:9443','cloud_account':False,'model_cloud_allowed':False},f);f.write('\n');f.flush();os.fsync(f.fileno())
    fd=os.open(marker.parent,os.O_DIRECTORY);os.fsync(fd);os.close(fd)
    print('Owner initialized. Pairing grant is private at /etc/kane-system/owner/first-pairing.json (expires).')
    print('No task has been resumed. Use sudo kanesys --config /etc/kane-system/core.env status.')
def main():
    if os.geteuid()!=0 or Path('/proc/1/comm').read_text().strip()!='systemd':raise RuntimeError('Native root console required')
    role=Path('/usr/share/kane-system/role').read_text().strip()
    if role=='installer':
        print('Kane System offline installer. Only a completely empty disk is accepted.',flush=True)
        run('lsblk','-o','NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS')
        disk=input('Empty target disk (absolute /dev/...): ').strip()
        confirm=input(f'Type ERASE:{disk} to confirm: ').strip()
        run('/usr/bin/kanesys','os','install','/opt/kane-installer/bundle','--disk',disk,'--erase-confirmation',confirm)
        print('Installation finished. Power off, detach installer media, then boot the target disk.',flush=True)
        return
    if role!='installed':raise RuntimeError('Unknown OS role; fail closed')
    initialize()
if __name__=='__main__':
    try:main()
    except (Exception,KeyboardInterrupt) as e:
        print(f'Setup stopped safely: {e}',file=sys.stderr,flush=True);sys.exit(1)
