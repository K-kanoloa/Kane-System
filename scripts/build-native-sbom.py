#!/usr/bin/env python3
"""Inventory a built native root, retaining component license boundaries.
This is evidence collection, not a vulnerability verdict or relicensing action.
"""
import argparse,hashlib,json,subprocess,tarfile
from pathlib import Path

def digest(p):
 with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('build',type=Path);ap.add_argument('--cargo-metadata',type=Path,required=True);ap.add_argument('--payload',type=Path,help='Exact retained payload when compact build removed its redundant external copy');ap.add_argument('--output',type=Path,required=True);a=ap.parse_args();a.output.mkdir(mode=0o700,parents=True,exist_ok=False)
 payload=a.payload or a.build/'rootfs.tar.zst'
 manifest=json.loads((a.build/'manifest.json').read_text())
 assert digest(payload)==manifest['payload_sha256'],'inventory requires the exact signed payload'
 inputs=json.loads((a.build/'inputs.json').read_text());rust=json.loads(a.cargo_metadata.read_text())
 components=[];notices=a.output/'rust-notices';notices.mkdir()
 for p in sorted(rust['packages'],key=lambda x:(x['name'],x['version'])):
  root=Path(p['manifest_path']).parent
  item={'type':'rust-crate','name':p['name'],'version':p['version'],'license_expression':p.get('license'),'source':p.get('source'),'manifest_sha256':digest(Path(p['manifest_path'])),'notices':[]}
  for f in sorted(root.iterdir()):
   if f.is_file() and f.name.upper().startswith(('LICENSE','COPYING','NOTICE','COPYRIGHT','UNLICENSE')):
    data=f.read_bytes();target=notices/(p['name']+'-'+p['version']+'-'+f.name);target.write_bytes(data);item['notices'].append({'path':str(target.relative_to(a.output)),'sha256':hashlib.sha256(data).hexdigest()})
  if p['name'].startswith('kane-'):item['licensing']='Root PolyForm preserved; independent System final license pending Owner; no blanket MIT grant'
  components.append(item)
 files=[];packages=[];unresolved=[]
 process=subprocess.Popen(['zstd','-dc',str(payload)],stdout=subprocess.PIPE)
 with tarfile.open(fileobj=process.stdout,mode='r|') as archive:
  for m in archive:
   name=m.name.removeprefix('./')
   row={'path':name,'type':m.type.decode('ascii',errors='replace'),'size':m.size,'mode':m.mode,'uid':m.uid,'gid':m.gid}
   if m.isfile():
    f=archive.extractfile(m);h=hashlib.sha256();collected=bytearray();capture=(name in ['usr/share/kane-system/packages.tsv','usr/share/kane-system/native-extras.json'] or (name.endswith('/package.json') and '/node_modules/' in name) or name.endswith('.dist-info/METADATA'))
    while b:=f.read(1048576):
     h.update(b)
     if capture and len(collected)<2_000_000:collected.extend(b[:2_000_000-len(collected)])
    row['sha256']=h.hexdigest()
    if name=='usr/share/kane-system/packages.tsv':packages=[line.split('\t') for line in collected.decode().splitlines()]
    if name=='usr/share/kane-system/native-extras.json':components.append({'type':'vendored-optional-runtime','provenance':json.loads(collected),'metadata_sha256':row['sha256'],'not_a_dpkg_install':True})
    if name.endswith('/package.json') and '/node_modules/' in name:
     try:
      pkg=json.loads(collected)
      if pkg.get('name') and pkg.get('version'):components.append({'type':'npm','name':pkg['name'],'version':pkg['version'],'license_expression':pkg.get('license'),'root_path':name,'sha256':row['sha256']})
     except (ValueError,UnicodeError):unresolved.append({'path':name,'reason':'not package metadata / bounded parse failed'})
    if name.endswith('.dist-info/METADATA'):
     fields={}
     for line in collected.decode(errors='replace').splitlines():
      if ': ' in line:
       key,value=line.split(': ',1)
       if key in ['Name','Version','License','License-Expression']:fields[key]=value
     components.append({'type':'python-distribution','metadata':fields,'root_path':name,'sha256':row['sha256']})
   elif m.issym() or m.islnk():row['link']=m.linkname
   files.append(row)
 assert process.wait()==0
 for name,version,arch in packages:components.append({'type':'distro-package','name':name,'version':version,'architecture':arch,'license_notice_root':'/usr/share/doc/'+name+'/copyright'})
 (a.output/'root-files.json').write_text(json.dumps(files,indent=2)+'\n')
 result={'format':'Kane native evidence inventory v1; not SPDX certification','build_inputs':inputs,'payload_sha256':manifest['payload_sha256'],'components':components,'unresolved_metadata':unresolved,'security_scan':'UNKNOWN: inventory is not a vulnerability audit','licensing':'Existing PolyForm and third-party licenses preserved; system license undecided','distribution_status':'Private local reference only; no publishing authorization','source_obligations':'Retain exact distro versions and third-party notices; corresponding-source/release obligations require explicit review before redistribution'}
 (a.output/'sbom.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({'components':len(components),'root_entries':len(files),'unresolved':len(unresolved),'output':str(a.output)}))
if __name__=='__main__':main()
