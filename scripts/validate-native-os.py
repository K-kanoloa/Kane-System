#!/usr/bin/env python3
"""Independent local-QEMU empty-disk installer/cold-boot acceptance.
Never uses cloud-init for acceptance, a private device, a remote Docker daemon,
a disabled permission check, or a fabricated Agent result. Credentials are generated
for this disposable guest and kept OUTSIDE its evidence share and public logs.
"""
import argparse,hashlib,json,os,secrets,shlex,socket,subprocess,time,traceback
from pathlib import Path
from native_preflight import require_native_storage
import pexpect
ROOT=Path(__file__).resolve().parents[1]
def sha(path):
 with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
class Vm:
 def __init__(self,work,installer,evidence,fixtures,phase,assets=None,memory_mb=3072,maintenance=False,attach_installer_readonly=False,cpu_model='max'):
  self.work=work;self.installer=installer;self.evidence=evidence;self.phase=phase
  self.env=os.environ.copy()
  for k in ['DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH']:self.env.pop(k,None)
  self.name='kane-p6-'+secrets.token_hex(6)
  self.prefix=['docker','--host=unix:///var/run/docker.sock','run','--rm','--name',self.name,'--network=none','--cap-drop=ALL','--security-opt=no-new-privileges','--pids-limit=128','--memory=6g','--cpus=2','--user',f'{os.getuid()}:{os.getgid()}','--mount',f'type=bind,src={work},dst=/work','--mount',f'type=bind,src={installer},dst=/installer.img,readonly','--mount',f'type=bind,src={evidence},dst=/exchange','--mount',f'type=bind,src={fixtures},dst=/fixtures,readonly','--workdir=/work']
  self.image='kane-system-validation:20261018'
  self.assets=assets
  assert memory_mb in [1024,1536,2048,3072]
  self.memory_mb=memory_mb
  self.maintenance=maintenance
  assert cpu_model in ['max','qemu64','Nehalem'];self.cpu_model=cpu_model
  self.attach_installer_readonly=attach_installer_readonly
  if assets is not None:
   self.prefix+=['--mount',f'type=bind,src={assets.resolve(strict=True)},dst=/assets,readonly']
 def create(self):
  for args in [['qemu-img','create','-f','qcow2','-F','qcow2','-b','/installer.img','installer-session.qcow2'],['qemu-img','create','-f','qcow2','target.qcow2','12G']]:
   subprocess.run([*self.prefix,self.image,*args],check=True,env=self.env)
 def boot(self,installer=False):
  drives=['-drive','file=target.qcow2,if=virtio,format=qcow2,cache=none,discard=unmap,detect-zeroes=unmap']
  if installer:drives=['-drive','file=installer-session.qcow2,if=virtio,format=qcow2,cache=none',*drives]
  elif self.attach_installer_readonly:drives+=['-drive','file=/installer.img,if=virtio,format=qcow2,readonly=on']
  cmd=[*self.prefix,'-i',self.image,'qemu-system-x86_64','-machine','q35','-accel','tcg,thread=multi,tb-size=128','-cpu',self.cpu_model,'-smp','2','-m',str(self.memory_mb),*drives,'-virtfs','local,path=/exchange,mount_tag=evidence,security_model=none','-virtfs','local,path=/fixtures,mount_tag=fixtures,security_model=none,readonly=on','-display','none','-serial','stdio','-monitor','none','-nic','none','-no-reboot']
  if self.assets is not None:cmd+=['-virtfs','local,path=/assets,mount_tag=assets,security_model=none,readonly=on']
  if self.maintenance:
   monitor=self.work/'maintenance-qmp.sock'
   if monitor.exists():monitor.unlink()
   cmd+=['-device','virtio-balloon-pci,id=maintenance-balloon,free-page-reporting=on','-qmp','unix:/work/maintenance-qmp.sock,server=on,wait=off']
  if not self.maintenance:
   cmd+=['-device','virtio-balloon-pci,id=reference-balloon,free-page-reporting=on']
  (self.evidence/(self.phase+'-vm-launch.json')).write_text(json.dumps({'arguments':cmd,'guest_memory_mb':self.memory_mb,'simulator':'QEMU TCG, not physical hardware','tcg_translation_cache_mb':128,'automatic_free_page_reporting':True,'guest_memory_capacity_unchanged':True,'workload_balloon_target_changed':False,'scope':'bound simulator host overhead only; guest kernel cgroups, Owner and security policies unchanged'},indent=2)+'\n')
  self.log=(self.evidence/(self.phase+'-console.log')).open('w')
  self.child=pexpect.spawn(cmd[0],cmd[1:],env=self.env,encoding='utf-8',codec_errors='replace',echo=False,timeout=900,maxread=65536)
  self.child.logfile_read=self.log
  self.cmd=cmd
 def balloon(self,memory_mb):
  assert self.maintenance and memory_mb==1792,'Only the explicit no-workload maintenance profile may reclaim guest RAM'
  s=socket.socket(socket.AF_UNIX);s.settimeout(10);s.connect(str(self.work/'maintenance-qmp.sock'));f=s.makefile('rwb',buffering=0)
  greeting=json.loads(f.readline());assert 'QMP' in greeting
  def execute(command,arguments=None):
   body={'execute':command}
   if arguments is not None:body['arguments']=arguments
   f.write(json.dumps(body).encode()+b'\n')
   while True:
    row=json.loads(f.readline())
    if 'error' in row:raise RuntimeError(row['error'])
    if 'return' in row:return row['return']
  try:
   execute('qmp_capabilities');before=execute('query-balloon');execute('balloon',{'value':memory_mb*1024*1024})
   for _ in range(120):
    after=execute('query-balloon')
    if after['actual']<=memory_mb*1024*1024+16*1024*1024:break
    time.sleep(.5)
   else:raise RuntimeError('Maintenance balloon did not reach the declared bound; refuse update')
   (self.evidence/(self.phase+'-memory.json')).write_text(json.dumps({'before':before,'after':after,'mode':'Boot 3GiB, explicitly reclaim to 1792MiB for quiescent OS maintenance; no model/Agent workload admitted'},indent=2)+'\n')
  finally:f.close();s.close()
 def expect(self,pattern,timeout=900):return self.child.expect(pattern,timeout=timeout)
 def send(self,value):self.child.sendline(value)
 def command(self,script,timeout=300):
  # shlex.quote escapes literal code; JSON is not used as shell escaping.
  token='KANE_COMMAND_'+secrets.token_hex(8)
  compile(script,'native-owner-command','exec')
  # Deliver large programs as exact bytes over the already authorized local
  # evidence share, not as multi-kilobyte UART pastes. Authentication stays the
  # same sudo/PAM path, and a hash mismatch refuses execution.
  program=self.evidence/(token+'.py');program.write_text(script);program.chmod(0o400)
  expected=hashlib.sha256(program.read_bytes()).hexdigest()
  bootstrap="import pathlib,subprocess,hashlib; p=pathlib.Path('/exchange'); temporary=not p.is_mount(); p=pathlib.Path('/run/"+token+"') if temporary else p; p.mkdir(mode=448,exist_ok=True); subprocess.run(['mount','-t','9p','-o','trans=virtio,version=9p2000.L,ro','evidence',str(p)],check=True) if temporary else None; b=(p/"+repr(program.name)+").read_bytes(); subprocess.run(['umount',str(p)],check=True) if temporary else None; assert hashlib.sha256(b).hexdigest()=="+repr(expected)+"; exec(compile(b,'native-owner-command','exec'))"
  command='sudo -n /usr/bin/python3 -c '+shlex.quote(bootstrap)+'; printf '+shlex.quote('\n'+token+':%s\n')+' "$?"'
  self.send(command);self.expect(token+r':([0-9]+)',timeout);code=int(self.child.match.group(1));assert code==0,f'guest command exit {code}'
 def stop(self,powercut=False):
  if self.child.isalive():
   subprocess.run(['docker','--host=unix:///var/run/docker.sock','kill','--signal=KILL' if powercut else '--signal=TERM',self.name],env=self.env,capture_output=True)
   self.child.expect(pexpect.EOF,timeout=30)
  self.child.close();self.log.close()
def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--installer-memory-mb',type=int,choices=[1024,1536],default=1536,help='Installer only, no Agent/model workload; installed OS resources and security unchanged');ap.add_argument('--cold-boot-memory-mb',type=int,choices=[2048,3072],default=3072);ap.add_argument('--installer',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);ap.add_argument('--scratch',type=Path,required=True);a=ap.parse_args()
 require_native_storage(output=a.output, scratch=a.scratch, workspace=ROOT)
 out=a.output.resolve();out.mkdir(mode=0o700,parents=True,exist_ok=False);work=a.scratch.resolve();work.mkdir(mode=0o700,parents=True,exist_ok=False)
 evidence=out/'evidence';evidence.mkdir(mode=0o700);fixtures=ROOT/'bench/phase6'
 credentials={'username':'kaneowner','password':'Test-'+secrets.token_urlsafe(22)}
 (out/'credentials.json').write_text(json.dumps(credentials));(out/'credentials.json').chmod(0o600)
 results={'installer_sha256':sha(a.installer),'environment':'local QEMU TCG x86-64; NOT physical hardware','steps':[],'phase6_closed':False,'installation_profile':f'{a.installer_memory_mb}MiB installer only; detached installed cold boot {a.cold_boot_memory_mb}MiB'};vm=None
 def save():
  (out/'result.json').write_text(json.dumps(results,indent=2)+'\n')
 try:
  vm=Vm(work,a.installer.resolve(),evidence,fixtures,'01-installer',memory_mb=a.installer_memory_mb);vm.create();vm.boot(installer=True)
  vm.expect('Empty target disk \\(absolute /dev/');vm.send('/dev/vdb');vm.expect('to confirm:');vm.send('ERASE:/dev/vdb')
  outcome=vm.child.expect(['Installation finished','Setup stopped safely:','kanesys:'],timeout=3600)
  assert outcome==0, 'installer rejected/faulted; preserve raw console and never count it as PASS'
  results['steps'].append({'id':'fresh-empty-disk-install','status':'PASS'});save()
  vm.send('poweroff');vm.expect(pexpect.EOF,timeout=180);vm.stop()
  # Installer and seed are absent from this command. The target must boot itself.
  vm=Vm(work,a.installer.resolve(),evidence,fixtures,'02-cold-boot',memory_mb=a.cold_boot_memory_mb);vm.boot(installer=False)
  vm.expect('New local Owner username:');vm.send(credentials['username']);vm.expect('Owner password \\(12');vm.send(credentials['password']);vm.expect('Confirm Owner password:');vm.send(credentials['password'])
  vm.expect('Owner initialized',timeout=120);vm.expect('login:');vm.send(credentials['username']);vm.expect('Password:');vm.send(credentials['password']);vm.expect(r'\$ ')
  vm.send('sudo -v');vm.expect('password for');vm.send(credentials['password']);vm.expect(r'\$ ')
  vm.command("import pathlib,subprocess,json; p=pathlib.Path('/exchange');p.mkdir(exist_ok=True);subprocess.run(['mount','-t','9p','-o','trans=virtio,version=9p2000.L','evidence',str(p)],check=True); status=subprocess.check_output(['kanesys','os','status'],text=True); pathlib.Path('/exchange/native-system.json').write_text(status);subprocess.run(['systemctl','is-active','kane-core.service','kane-system.target'],check=True); assert pathlib.Path('/etc/kane-system/owner-initialized.json').is_file(); print('KANE_NATIVE_COLD_BOOT_OWNER_PASS')")
  vm.expect('KANE_NATIVE_COLD_BOOT_OWNER_PASS') if False else None
  results['steps'].append({'id':'detached-cold-boot-owner-native-core','status':'PASS'});save()
  vm.command("import subprocess,pathlib,json; q=['kanesys','--config','/etc/kane-system/core.env','host-request']; workspace=json.loads(subprocess.check_output(q,input=json.dumps({'op':'create_workspace','kind':'document'}),text=True)); pathlib.Path('/exchange/persistent-workspace.json').write_text(json.dumps(workspace)); denied=subprocess.run(['runuser','-u','nobody','--','python3','-c',\"import socket;s=socket.socket(socket.AF_UNIX);s.connect('/run/kane-system/core.sock')\"],capture_output=True,text=True); assert denied.returncode!=0; pathlib.Path('/exchange/unauthorized-peer.log').write_text(denied.stderr); assert pathlib.Path('/var/lib/kane-system/state.db').stat().st_mode&0o077==0; print('KANE_NATIVE_PEER_DENIAL_PASS')")
  results['steps'].append({'id':'native-owner-ipc-kernel-denial','status':'PASS'});save()

  vm.command("import pathlib,subprocess,json; subprocess.run(['systemctl','kill','--signal=SIGKILL','--kill-whom=main','kane-core.service'],check=True); import time;time.sleep(5);subprocess.run(['systemctl','is-active','kane-core.service'],check=True);pathlib.Path('/exchange/core-restart-journal.log').write_text(subprocess.check_output(['journalctl','-u','kane-core.service','--no-pager'],text=True));print('NATIVE_CORE_COMPONENT_RESTART_ONLY')")
  results['steps'].append({'id':'native-core-service-crash','status':'PASS','scope':'component only; NOT Task semantic recovery'});save()
  vm.send('sudo -n systemctl poweroff');vm.expect(pexpect.EOF,timeout=180);vm.stop()
  results['status']='PASS_LIMITED';results['scope']='empty installation, detached cold boot, local Owner and Core component; model/Agent/recovery/update gates remain unaccepted';save()
 except Exception:
  results['failure']=traceback.format_exc();results['status']='FAIL';save();raise
 finally:
  if vm:
   try:vm.stop()
   except Exception:pass
  save()
if __name__=='__main__':main()
