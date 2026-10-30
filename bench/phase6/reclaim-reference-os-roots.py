#!/usr/bin/python3
"""Explicit Owner maintenance of ONLY this disposable reference's obsolete OS roots.
Keeps current A/B, all @data/checkpoints/snapshots and every Task/Artifact intact.
Not a production garbage collector or a Task replay/reconciliation operation.
"""
import json,os,re,subprocess
from pathlib import Path
assert os.geteuid()==0
assert Path('/usr/share/kane-system/role').read_text().strip()=='installed'
assert json.loads(Path('/usr/share/kane-system/build.json').read_text())['reference_only']
assert Path('/etc/kane-system/reference-nativehello.json').is_file(),'only this explicitly configured synthetic acceptance guest'
assert not subprocess.check_output(['systemctl','list-units','--no-legend','--state=active','kane-agent@*.service','kane-model@*.service'],text=True)
def text(*args):return subprocess.check_output(args,text=True).strip()
def rpc(op,**kw):return json.loads(subprocess.check_output(['kanesys','--config','/etc/kane-system/core.env','host-request'],input=json.dumps({'op':op,**kw}),text=True))
def state():
 tasks=rpc('tasks');return {'tasks':tasks,'budgets':{t['spec']['budget_id']:rpc('model_budget',id=t['spec']['budget_id']) for t in tasks},'artifacts':{t['id']:rpc('artifacts',id=t['id']) for t in tasks},'models':rpc('model_instances'),'machine':Path('/etc/machine-id').read_text()}
before=state();active=text('findmnt','-n','-o','FSROOT','/');assert active in ['/@slots/A','/@slots/B']
top=Path('/run/kane-reference-root-retention');top.mkdir(mode=0o700)
device=text('findmnt','-n','-o','UUID','/');assert re.fullmatch('[a-f0-9-]{36}',device)
subprocess.run(['mount','-t','btrfs','-o','subvolid=5','UUID='+device,str(top)],check=True)
removed=[]
try:
 selected=dict(x.split('=',1) for x in text('grub-editenv',str(top/'@boot/grub/grubenv'),'list').splitlines() if '=' in x).get('saved_entry');assert selected==('0' if active.endswith('A') else '1')
 key='/usr/share/kane-system/trust/release.pem'
 def verified(path):
  root=path/'usr/share/kane-system';subprocess.run(['openssl','pkeyutl','-verify','-pubin','-inkey',key,'-rawin','-in',str(root/'installed-manifest.json'),'-sigfile',str(root/'installed-manifest.sig')],check=True)
  return json.loads((root/'installed-manifest.json').read_text())
 kept={slot:verified(top/'@slots'/slot) for slot in ['A','B']};minimum=min(m['sequence'] for m in kept.values())
 assert kept[active[-1]]['sequence']>=9 and minimum>=7
 journal=top/'@data/reference-root-retention.json'
 for path in sorted((top/'@slots').iterdir()):
  if not re.fullmatch('previous-[a-f0-9]{32}',path.name):continue
  assert not path.is_symlink() and path.is_dir();old=verified(path)
  if old['sequence']>=minimum:continue
  assert not (path/'etc/kane-system/owner-initialized.json').exists(),'Owner configuration in a supposed OS-only root; refuse'
  entry={'name':path.name,'manifest':old,'state':'authorized-before-delete'}
  with journal.open('w') as f:os.fchmod(f.fileno(),0o600);json.dump({'kept':kept,'previous':removed,'current':entry,'data_snapshots_untouched':True},f);f.flush();os.fsync(f.fileno())
  subprocess.run(['btrfs','subvolume','show',str(path)],check=True);subprocess.run(['btrfs','subvolume','delete',str(path)],check=True)
  entry['state']='obsolete-os-root-deleted';removed.append(entry)
 # Deletion commits before the asynchronous btrfs cleaner has reclaimed every
 # extent. Wait for that existing kernel work before asking QEMU to discard it.
 subprocess.run(['btrfs','subvolume','sync',str(top)],check=True)
 subprocess.run(['sync'],check=True)
finally:subprocess.run(['umount',str(top)],check=True);top.rmdir()
trim=text('fstrim','-v','/');assert state()==before,'User/system objects changed during OS-only retention maintenance'
Path('/exchange/storage-maintenance.json').write_text(json.dumps({'active':active,'kept_sequences':{x:m['sequence'] for x,m in kept.items()},'removed':removed,'trim':trim,'data_and_objects_equal':True,'data_snapshots_deleted':False,'scope':'only synthetic reference obsolete signed OS roots; current A/B and all persistent data retained'},indent=2))
