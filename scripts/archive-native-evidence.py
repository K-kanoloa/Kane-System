#!/usr/bin/env python3
"""Hash local native evidence and protected source; never export private credentials.
Read-only except the NEW output directory. No Git index/commit or phase closure.
"""
import argparse,datetime,hashlib,json,os,subprocess
from pathlib import Path
R=Path(__file__).resolve().parents[1];P=R/'.kane/phase6'
def sha(p):
 with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--output',type=Path,required=True);ap.add_argument('--phase-state',type=Path,required=True,help='Private stage-state JSON supplied locally; never published');a=ap.parse_args();out=a.output.resolve();out.mkdir(mode=0o700,parents=True,exist_ok=False)
 before=json.loads((P/'before.json').read_text());missing=[];changed=[]
 for n,h in before['files'].items():
  p=R/n
  if not p.is_file():missing.append(n)
  elif sha(p)!=h:changed.append(n)
 head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=R,text=True).strip();index=sha(R/'.git/index');state=json.loads(a.phase_state.read_text())
 history_unchanged={f'phase{i}':state.get(f'phase{i}')==before['phase_status'].get(f'phase{i}') for i in range(6)}
 audit={'time_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'original_files':len(before['files']),'missing':missing,'changed_since_before':changed,'head':head,'head_unchanged':head==before['head'],'index_sha256':index,'index_unchanged':index==before['index_sha256'],'historical_phase_entries_unchanged':history_unchanged,'phase_flags':{f'phase{i}_closed':state.get(f'phase{i}_closed') for i in range(7)},'license_unchanged':sha(R/'LICENSE')==before['files']['LICENSE'],'scope':'source and historical phase state only; no private device access'}
 (out/'protected-state.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2)+'\n')
 assert not missing and audit['head_unchanged'] and audit['index_unchanged'] and audit['license_unchanged'] and all(history_unchanged.values())
 rows=[];seen={};results=[]
 for p in sorted(P.rglob('*')):
  if not p.is_file() or out in p.parents:continue
  rel=p.relative_to(P)
  if any(x in ['private-inputs','signing'] for x in rel.parts) or p.name in ['credentials.json','password','local-reference.pem','recovery.pem']:continue
  resolved=p.resolve(strict=True)
  h=seen.get(resolved)
  if h is None:h=sha(p);seen[resolved]=h
  row={'path':str(p.relative_to(R)),'bytes':p.stat().st_size,'sha256':h}
  if p.is_symlink():row['retained_exact_target']=str(resolved)
  rows.append(row)
  if p.name in ['result.json','build-result.json']:
   try:
    x=json.loads(p.read_text());results.append({'path':row['path'],'status':x.get('status'),'passed':x.get('passed'),'scope':x.get('scope'),'sha256':h})
   except (ValueError,UnicodeError):pass
 (out/'files.json').write_text(json.dumps(rows,indent=2)+'\n');(out/'attempts.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
 (out/'SHA256SUMS').write_text(''.join(f"{x['sha256']}  {x['path']}\n" for x in rows))
 print(json.dumps({'files':len(rows),'attempts':len(results),'protected_audit':'PASS','output':str(out)}))
if __name__=='__main__':main()
