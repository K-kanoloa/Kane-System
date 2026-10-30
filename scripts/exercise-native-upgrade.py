#!/usr/bin/env python3
"""Owner-authenticated signed native-slot upgrade/rollback on an installed VM.
Update media must be root-owned, read-only, and signed by the installed trust key.
No data rollback, SQL writes, permission bypass or Task replay.
"""
import argparse,json,traceback
from pathlib import Path
from native_preflight import require_native_storage
import pexpect
from validate_native_import import Vm,ROOT
from importlib.util import spec_from_file_location,module_from_spec
s=spec_from_file_location('native_exercise',Path(__file__).with_name('exercise-native-system.py'));m=module_from_spec(s);s.loader.exec_module(m)

def main():
 ap=argparse.ArgumentParser(description=__doc__)
 for name in ['acceptance','installer','scratch','assets','output']:ap.add_argument('--'+name,type=Path,required=True)
 ap.add_argument('--interrupt-staging',action='store_true',help='Actually SIGKILL the local VM during populated unpublished upgrade staging, verify prior slot/current data on new boot, then retry through original authority')
 ap.add_argument('--discard-unpublished',action='store_true',help='Owner-authorized removal of the exact journal-identified never-selected partial OS stage; no user-data deletion')
 ap.add_argument('--shared-payload',action='store_true',help='Read-only bind one real signed good payload into negative fixtures; preserve full native verification and root ownership')
 ap.add_argument('--installer-payload',action='store_true',help='Use the exact signed payload on read-only attached installer media; no duplicate host payload storage')
 ap.add_argument('--maintenance-memory-mb',type=int,choices=[1536,3072],default=3072,help='Quiescent OS maintenance only; never starts model/Agent workloads')
 ap.add_argument('--require-outcomes',action='store_true',help='Require actual Done and unresolved usage before the data-preservation regression')
 a=ap.parse_args();require_native_storage(output=a.output, scratch=a.scratch, workspace=ROOT);assert not (a.shared_payload and a.installer_payload),'choose one immutable payload transport';out=a.output.resolve();out.mkdir(mode=0o700,parents=True,exist_ok=False);e=out/'evidence';e.mkdir(mode=0o700)
 creds=json.loads((a.acceptance/'credentials.json').read_text());result={'status':'IN_PROGRESS','scope':'actual local QEMU installed native OS; same-schema signed root update','phase6_closed':False,'steps':[]};vm=None
 def boot(label):
  v=Vm(a.scratch.resolve(),a.installer.resolve(),e,ROOT/'bench/phase6',label,a.assets.resolve(),memory_mb=a.maintenance_memory_mb,maintenance=a.maintenance_memory_mb==3072,attach_installer_readonly=a.installer_payload);v.boot();m.login(v,creds)
  v.command("import pathlib,subprocess\nfor tag,path in [('evidence','/exchange'),('assets','/assets')]:\n pathlib.Path(path).mkdir(exist_ok=True);subprocess.run(['mount','-t','9p','-o','trans=virtio,version=9p2000.L'+(',ro' if tag=='assets' else ''),tag,path],check=True)")
  v.command("import subprocess;assert not subprocess.check_output(['systemctl','list-units','--no-legend','--state=active','kane-agent@*.service','kane-model@*.service'],text=True);subprocess.run(['modprobe','virtio_balloon'],check=True);open('/proc/sys/vm/drop_caches','w').write('3')")
  if a.maintenance_memory_mb==3072:v.balloon(1792)
  if a.installer_payload:
   v.command("""import pathlib,subprocess,json
media=pathlib.Path('/media/kane-reference-installer');media.mkdir(mode=0o700,exist_ok=True)
assert pathlib.Path('/sys/block/vdb/ro').read_text().strip()=='1'
subprocess.run(['mount','-t','ext4','-o','ro,noload','/dev/vdb1',str(media)],check=True)
assert (media/'usr/share/kane-system/role').read_text().strip()=='installer'
assert not (media/'etc/kane-system/owner-initialized.json').exists()
source=media/'opt/kane-installer/bundle';original=json.loads((source/'manifest.json').read_text());new=json.loads(pathlib.Path('/assets/good/manifest.json').read_text())
assert original['payload_sha256']==new['payload_sha256'] and original['source_fingerprint']==new['source_fingerprint']
for name in ['good','bad-signature','incompatible-schema']:
 target=pathlib.Path('/assets')/name/'rootfs.tar.zst';assert target.is_file()
 subprocess.run(['mount','--bind',str(source/'rootfs.tar.zst'),str(target)],check=True)
 subprocess.run(['mount','-o','remount,bind,ro',str(target)],check=True)
pathlib.Path('/exchange/readonly-install-media.json').write_text(json.dumps({'actual_readonly_block_device':True,'payload_sha256':original['payload_sha256'],'source_fingerprint':original['source_fingerprint'],'scope':'existing complete payload; private release-envelope fixtures; no payload mutation or signature bypass'},indent=2))
""",timeout=180)
  if a.shared_payload:
   v.command("""import pathlib,subprocess,json,hashlib
source=pathlib.Path('/assets/good/rootfs.tar.zst');meta=json.loads(pathlib.Path('/assets/good/manifest.json').read_text())
assert source.is_file() and not source.is_symlink() and source.stat().st_uid==0 and source.stat().st_nlink==1 and source.stat().st_mode&0o022==0
with source.open('rb') as f:assert hashlib.file_digest(f,'sha256').hexdigest()==meta['payload_sha256']
for name in ['bad-signature','incompatible-schema']:
 target=pathlib.Path('/assets')/name/'rootfs.tar.zst';assert target.is_file() and not target.is_symlink() and target.stat().st_uid==0 and target.stat().st_size==0
 subprocess.run(['mount','--bind',str(source),str(target)],check=True)
 subprocess.run(['mount','-o','remount,bind,ro',str(target)],check=True)
pathlib.Path('/exchange/shared-real-update-payload.json').write_text(json.dumps({'real_payload_sha256':meta['payload_sha256'],'all_software_files_are_real':True,'read_only_bind_for_negative_fixtures':True,'signature_schema_owner_and_content_checks_unchanged':True}))
""",timeout=180)
  v.command("import pathlib,subprocess,json;before=subprocess.check_output(['findmnt','-n','-o','OPTIONS','/'],text=True);subprocess.run(['mount','-o','remount,compress=zstd:3','/'],check=True);after=subprocess.check_output(['findmnt','-n','-o','OPTIONS','/'],text=True);assert 'compress=zstd:3' in after;pathlib.Path('/exchange/"+label+"-filesystem.json').write_text(json.dumps({'before':before,'after':after,'scope':'Owner-authenticated filesystem compression; no quota, permission or recovery policy disabled'},indent=2))")
  return v
 def shutdown(v):v.send('sudo -n systemctl poweroff');v.expect(pexpect.EOF,timeout=240);v.stop()
 def snapshot(v,label):
  v.command(f"""import pathlib,subprocess,json
q=['kanesys','--config','/etc/kane-system/core.env','host-request']
def rpc(op,**kw):return json.loads(subprocess.check_output(q,input=json.dumps({{'op':op,**kw}}),text=True))
tasks=rpc('tasks');agents=[rpc('runtime_status',id=p.stem)['agent'] for p in sorted(pathlib.Path('/etc/kane-system/runtimes').glob('*.json'))];budgets={{t['spec']['budget_id']:rpc('model_budget',id=t['spec']['budget_id']) for t in tasks}}
meta=json.loads(pathlib.Path('/usr/share/kane-system/installed-manifest.json').read_text())
r={{'manifest':meta,'tasks':tasks,'agents':agents,'budgets':budgets,'machine_id':pathlib.Path('/etc/machine-id').read_text(),'artifact_sets':{{t['id']:rpc('artifacts',id=t['id']) for t in tasks}},'native_status':json.loads(subprocess.check_output(['kanesys','os','status'],text=True))}}
pathlib.Path('/exchange/{label}.json').write_text(json.dumps(r,indent=2))
""",timeout=180)
 try:
  vm=boot('01-before-upgrade')
  if a.interrupt_staging:
   snapshot(vm,'before-interrupted-update')
   vm.command("""import subprocess,pathlib,json,time,os,signal
log=pathlib.Path('/exchange/interrupted-update.log').open('w')
p=subprocess.Popen(['kanesys','os','upgrade','/assets/good'],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
deadline=time.monotonic()+600
while time.monotonic()<deadline:
 journal=pathlib.Path('/.kane-data/os-update.json')
 if journal.exists():
  j=json.loads(journal.read_text());stage=pathlib.Path('/run/kane-os-top/@slots')/('stage-'+j.get('token',''))
  if j.get('phase')=='preparing' and stage.is_dir() and sum(1 for _ in stage.iterdir())>=3:
   os.killpg(p.pid,signal.SIGSTOP)
   assert p.poll() is None and json.loads(journal.read_text())['phase']=='preparing'
   pathlib.Path('/exchange/interruption-ready.json').write_text(json.dumps({'journal':j,'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text(),'active_root':subprocess.check_output(['findmnt','-n','-o','FSROOT','/'],text=True).strip(),'real_populated_stage':True,'updater_process_group_stopped_before_whole_VM_cut':p.pid},indent=2))
   break
 if p.poll() is not None:raise RuntimeError('Update finished or failed before real interruption; no fault-injection PASS')
 time.sleep(.1)
else:raise RuntimeError('No populated unpublished stage within bounded deadline')
""",timeout=660)
   vm.stop(powercut=True)
   (e/'actual-upgrade-cut.json').write_text(json.dumps({'method':'SIGKILL local QEMU container during real unpublished upgrade extraction','injected':True},indent=2))
   vm=boot('01-after-interrupted-update');snapshot(vm,'after-interrupted-update')
   original=json.loads((e/'before-interrupted-update.json').read_text());recovered=json.loads((e/'after-interrupted-update.json').read_text())
   for key in ['manifest','machine_id','tasks','budgets','artifact_sets']:assert recovered[key]==original[key], 'Interrupted update changed '+key
   vm.command("""import pathlib,json,subprocess
b=json.loads(pathlib.Path('/exchange/interruption-ready.json').read_text());assert pathlib.Path('/proc/sys/kernel/random/boot_id').read_text()!=b['boot_id'];assert subprocess.check_output(['findmnt','-n','-o','FSROOT','/'],text=True).strip()==b['active_root']
""")
   result['steps'].append({'id':'actual-upgrade-interruption-prior-slot-current-data','status':'PASS'})
   a.discard_unpublished=True
  if a.discard_unpublished:
   vm.command("""import pathlib,json,subprocess,re
j=json.loads(pathlib.Path('/.kane-data/os-update.json').read_text());active=subprocess.check_output(['findmnt','-n','-o','FSROOT','/'],text=True).strip();current=active[-1]
assert active in ['/@slots/A','/@slots/B'] and j['phase']=='preparing' and j['target_slot']!=current and re.fullmatch('[0-9a-f]{32}',j['token'])
boot=subprocess.check_output(['grub-editenv','/kane-boot/grub/grubenv','list'],text=True);assert 'saved_entry='+('0' if current=='A' else '1') in boot
p=pathlib.Path('/run/kane-stage-maintenance');p.mkdir(mode=0o700);device=subprocess.check_output(['findmnt','-n','-o','UUID','/'],text=True).strip()
subprocess.run(['mount','-t','btrfs','-o','subvolid=5','UUID='+device,str(p)],check=True)
try:
 stage=p/'@slots'/('stage-'+j['token']);assert stage.is_dir();subprocess.run(['btrfs','subvolume','show',str(stage)],check=True);subprocess.run(['btrfs','subvolume','delete',str(stage)],check=True)
finally:subprocess.run(['umount',str(p)],check=True);p.rmdir()
subprocess.run(['sync'],check=True);trim=subprocess.check_output(['fstrim','-v','/'],text=True)
pathlib.Path('/exchange/discarded-unpublished-stage.json').write_text(json.dumps({'prior_journal':j,'active_root_unchanged':active,'only_deleted':'@slots/stage-'+j['token'],'persistent_data_touched':False,'trim':trim},indent=2))
""",timeout=300)
  snapshot(vm,'before')
  if a.require_outcomes:
   b=json.loads((e/'before.json').read_text())
   assert any(t['state']=='Done' for t in b['tasks']) and any(x['reserved_tokens']>0 for x in b['budgets'].values()),'Real verified outcome plus retained Unknown required; empty-data tests do not qualify'
  vm.command("""import subprocess,pathlib,json
for name,reason in [('bad-signature',None),('incompatible-schema','data schema incompatible')]:
 p=subprocess.run(['kanesys','os','upgrade','/assets/'+name],capture_output=True,text=True)
 pathlib.Path('/exchange/denied-'+name+'.log').write_text(p.stdout+p.stderr)
 assert p.returncode!=0,'negative fixture unexpectedly admitted'
 if reason:assert reason in p.stderr,p.stderr
 subprocess.run(['systemctl','is-active','kane-core.service'],check=True)
""",timeout=180)
  result['steps'].append({'id':'signature-and-schema-denial','status':'PASS'})
  vm.command("import subprocess,pathlib;p=subprocess.run(['kanesys','os','upgrade','/assets/good'],capture_output=True,text=True);pathlib.Path('/exchange/upgrade.log').write_text(p.stdout+p.stderr);print(p.stdout,p.stderr);p.check_returncode()",timeout=2400)
  shutdown(vm);result['steps'].append({'id':'signed-inactive-slot-staging','status':'PASS'})
  vm=boot('02-updated-boot');snapshot(vm,'updated')
  vm.command("import subprocess,pathlib,json;r=json.loads(subprocess.check_output(['kane-system-status','--manifest'],text=True));assert r['sequence']==json.loads(pathlib.Path('/assets/good/manifest.json').read_text())['sequence'];pathlib.Path('/exchange/updated-diagnostic.json').write_text(json.dumps(r))")
  before=json.loads((e/'before.json').read_text());after=json.loads((e/'updated.json').read_text());assert after['manifest']['sequence']>before['manifest']['sequence'];assert after['machine_id']==before['machine_id'];assert after['tasks']==before['tasks'];assert after['budgets']==before['budgets'];assert after['artifact_sets']==before['artifact_sets'];assert {x['id'] for x in after['agents']}=={x['id'] for x in before['agents']}
  result['steps'].append({'id':'actual-updated-root-data-retained','status':'PASS'})
  vm.command("import subprocess,pathlib;p=subprocess.run(['kanesys','os','rollback'],capture_output=True,text=True);pathlib.Path('/exchange/rollback.log').write_text(p.stdout+p.stderr);print(p.stdout,p.stderr);p.check_returncode()",timeout=180)
  shutdown(vm);vm=boot('03-rollback-boot');snapshot(vm,'rolled-back');old=json.loads((e/'rolled-back.json').read_text());assert old['manifest']['sequence']==before['manifest']['sequence'];assert old['tasks']==before['tasks'] and old['budgets']==before['budgets'] and old['artifact_sets']==before['artifact_sets'] and old['machine_id']==before['machine_id']
  result['steps'].append({'id':'actual-compatible-previous-root-current-data','status':'PASS'})
  # Explicitly select the already tested newer slot again; no re-extraction,
  # inference, data restore or unknown refund. Both slot manifests are verified.
  vm.command("import subprocess;subprocess.run(['kanesys','os','rollback'],check=True)",timeout=180);shutdown(vm)
  vm=boot('04-return-updated-slot');snapshot(vm,'final');final=json.loads((e/'final.json').read_text());assert final['manifest']['sequence']==after['manifest']['sequence'];shutdown(vm)
  result['status']='PASS'
 except Exception:result.update(status='FAIL',failure=traceback.format_exc());raise
 finally:
  (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
  if vm:vm.stop()
if __name__=='__main__':main()
