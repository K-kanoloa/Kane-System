#!/usr/bin/env python3
"""Offline, unprivileged QEMU fixture for Rust storage admission and two-boot persistence."""
import argparse,base64,gzip,hashlib,json,os,pathlib,subprocess,time,uuid
ROOT=pathlib.Path(__file__).resolve().parents[1]

def read_result(log, boot):
    result=None
    for line in log.read_text(errors='replace').splitlines():
        if 'KANE_STORAGE_RESULT {' not in line: continue
        fragment=line.split('KANE_STORAGE_RESULT ',1)[1]
        try: candidate,_=json.JSONDecoder().raw_decode(fragment)
        except json.JSONDecodeError: continue
        if candidate.get('boot')==boot or candidate.get('passed') is False:
            result=candidate
    return result

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--image',type=pathlib.Path,required=True);p.add_argument('--image-sha256',required=True)
    p.add_argument('--binary',type=pathlib.Path,required=True);p.add_argument('--output',type=pathlib.Path,required=True)
    p.add_argument('--resume-second-boot',action='store_true',help='Preserve a completed first-boot log/disk; record recovered evidence separately')
    p.add_argument('--timeout',type=int,default=480);p.add_argument('--tools',default='kane-system-validation:20261018')
    a=p.parse_args();image=a.image.resolve(strict=True);binary=a.binary.resolve(strict=True)
    subprocess.run([str(binary),'storage-check','--help'],check=True,capture_output=True,timeout=10)
    digest=hashlib.sha256(image.read_bytes()).hexdigest();assert digest==a.image_sha256
    out=a.output.resolve()
    if not a.resume_second_boot: out.mkdir(parents=True,exist_ok=False)
    binary_data=binary.read_bytes()
    if not a.resume_second_boot:
        (out/'kanesys').write_bytes(binary_data)
        (out/'kanesys').chmod(0o700)
        writes=[]
        for dst,data,mode in [('/usr/local/libexec/storage-test.py',(ROOT/'scripts/validate-storage-guest.py').read_bytes(),'0700'),('/etc/kane-storage-validation',b'disposable-vm\n','0600')]:
            writes.append({'path':dst,'permissions':mode,'encoding':'gz+b64','content':base64.b64encode(gzip.compress(data,mtime=0)).decode()})
        # Put the binary on read-only media. A large base64 binary in cloud-init YAML
        # is very slow to parse under software emulation and adds no test coverage.
        config={'users':[],'ssh_pwauth':False,'disable_root':True,'package_update':False,'package_upgrade':False,'cloud_init_modules':['seed_random','write_files','growpart','resizefs','set_hostname'],'cloud_config_modules':['runcmd'],'cloud_final_modules':['scripts_user','final_message'],'write_files':writes,'runcmd':[['mkdir','-p','/mnt/validation-seed'],['mount','-o','ro','/dev/sr0','/mnt/validation-seed'],['cp','/mnt/validation-seed/kanesys','/usr/local/bin/kanesys'],['chmod','0755','/usr/local/bin/kanesys'],['umount','/mnt/validation-seed'],['python3','/usr/local/libexec/storage-test.py'],['systemctl','poweroff']]}
        (out/'user-data').write_text('#cloud-config\n'+json.dumps(config));(out/'meta-data').write_text(json.dumps({'instance-id':out.name,'local-hostname':'kane-storage-validation'}))
        (out/'network-config').write_text("version: 2\nethernets:\n  validation:\n    match: {name: 'en*'}\n    dhcp4: true\n    dhcp6: false\n    optional: true\n")
    prefix=['docker','run','--rm','--network=none','--user',f'{os.getuid()}:{os.getgid()}','--cap-drop=ALL','--security-opt=no-new-privileges','--pids-limit=128','--memory=2300m','--cpus=2','--mount',f'type=bind,src={out},dst=/work','--workdir=/work']
    if not a.resume_second_boot:
        subprocess.run([*prefix,a.tools,'genisoimage','-quiet','-output','seed.iso','-volid','cidata','-joliet','-rock','user-data','meta-data','network-config','kanesys'],check=True,timeout=30)
    base=['--mount',f'type=bind,src={image},dst=/base/cloud.img,readonly']
    if not a.resume_second_boot:
        subprocess.run([*prefix,*base,a.tools,'qemu-img','create','-f','qcow2','-F','qcow2','-b','/base/cloud.img','system.qcow2','8G'],check=True,timeout=30)
    name='kane-storage-vm-'+uuid.uuid4().hex[:12]
    cmd=[*prefix,*base,'--name',name,a.tools,'qemu-system-x86_64','-machine','q35','-accel','tcg,thread=multi','-cpu','max','-smp','2','-m','1536','-drive','file=system.qcow2,if=virtio,format=qcow2','-object','rng-random,id=rng0,filename=/dev/urandom','-device','virtio-rng-pci,rng=rng0','-drive','file=seed.iso,media=cdrom,readonly=on','-display','none','-serial','stdio','-monitor','none','-nic','user,model=virtio-net-pci,restrict=on','-no-reboot']
    report={'binary_sha256':hashlib.sha256(binary_data).hexdigest(),'image_sha256':digest,'boots':[],'tools_image':subprocess.check_output(['docker','image','inspect',a.tools,'--format','{{.Id}}'],text=True).strip()}
    if a.resume_second_boot:
        previous=json.loads((out/'result.json').read_text())
        assert previous['binary_sha256']==report['binary_sha256']==hashlib.sha256((out/'kanesys').read_bytes()).hexdigest()
        assert previous['image_sha256']==digest and previous['tools_image']==report['tools_image']
        assert not (out/'boot-2.log').exists(), 'Do not overwrite an earlier second boot'
        first=read_result(out/'boot-1.log',1)
        assert first and first.get('passed') and 'reboot: Power down' in (out/'boot-1.log').read_text(errors='replace')
        report['boots'].append({'exit_code':None,'seconds':None,'result':first,'recovered_from_completed_console':True,'host_exit_code_not_recorded':True})
    try:
        for boot in ([2] if a.resume_second_boot else [1,2]):
            start=time.monotonic();log=out/f'boot-{boot}.log'
            with log.open('x') as stream:
                try:code=subprocess.run(cmd,stdout=stream,stderr=subprocess.STDOUT,timeout=a.timeout).returncode
                except subprocess.TimeoutExpired:
                    subprocess.run(['docker','rm','--force',name],capture_output=True,timeout=20);code=124
            result=read_result(log,boot)
            report['boots'].append({'exit_code':code,'seconds':time.monotonic()-start,'result':result})
            if code!=0 or not result or not result.get('passed'):break
    finally:
        report['passed']=len(report['boots'])==2 and all((b['exit_code']==0 or b.get('recovered_from_completed_console')) and b['result'] and b['result'].get('passed') for b in report['boots'])
        (out/('resumed-result.json' if a.resume_second_boot else 'result.json')).write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
    return 0 if report['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
