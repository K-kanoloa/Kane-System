#!/usr/bin/env python3
"""One local regression run, explicit local opt-ins only; no private devices/providers.
Does not merge earlier attempts or close any phase. Kernel/remote opt-ins stay off.
"""
import argparse,os,json,hashlib,subprocess,time
from pathlib import Path
from native_preflight import require_native_storage
R=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--fixtures',type=Path,required=True,help='Local JSON mapping dependency environment names to path and sha256');p.add_argument('--output',type=Path,required=True);p.add_argument('--temp',type=Path,required=True);p.add_argument('--browser-cache',type=Path);p.add_argument('--source-map',type=Path,required=True);p.add_argument('--full-source-freeze',type=Path,required=True);a=p.parse_args();require_native_storage(output=a.output, scratch=a.temp, workspace=R);o=a.output.resolve();o.mkdir(mode=0o700,parents=True,exist_ok=False)
assert not a.temp.exists(),'never overwrite previous test evidence'
e=os.environ.copy()
for k in list(e):
 if k.startswith(('KANE_','DOCKER_','PLAYWRIGHT_')):e.pop(k)
e.update(DOCKER_HOST='unix:///var/run/docker.sock',PYTHONPATH=str(R/'apps/api')+':'+str(R/'apps/api/tests'))
flags=['KANE_PR1_TEST','KANE_PR2_TEST','KANE_PR3A_TEST','KANE_PR3B_TEST','KANE_PR4_TEST','KANE_FIFO_TEST','KANE_PHASE1_TEST','KANE_PHASE2_TEST','KANE_PHASE2_FINAL_TEST','KANE_PHASE3_TEST','KANE_PHASE5_TEST']
if a.browser_cache:
 assert a.browser_cache.is_dir();e['PLAYWRIGHT_BROWSERS_PATH']=str(a.browser_cache.resolve());flags.append('KANE_PHASE4_TEST')
for k in flags:e[k]='1'
def sha(f):
 with Path(f).open('rb') as h:return hashlib.file_digest(h,'sha256').hexdigest()
fixtures=json.loads(a.fixtures.read_text())
model_names=['KANE_LOCAL_MODEL_SERVER','KANE_LOCAL_MODEL_FILE','KANE_PHASE2_TOOL_MODEL_FILE']
pins=['KANE_PHASE3_SCHEMA7_BINARY','KANE_PREVIOUS_CORE_BINARY','KANE_PHASE5_SCHEMA8_BINARY']
for name in model_names+pins:
 item=fixtures[name];path=Path(item['path']).resolve(strict=True)
 assert len(item['sha256'])==64 and sha(path)==item['sha256'],'dependency fingerprint mismatch: '+name
 e[name]=str(path)
full_freeze=json.loads(a.full_source_freeze.read_text())
assert all(sha(R/n)==h for n,h in full_freeze.items()),'source differs from final full freeze'
record={'status':'IN_PROGRESS','scope':'ONE full collected Python suite with existing explicit authorized local integrations; no remote/private device or stored provider credentials','opt_ins':flags,'dependencies':{k:{'path':e[k],'sha256':sha(e[k])} for k in ['KANE_LOCAL_MODEL_SERVER','KANE_LOCAL_MODEL_FILE','KANE_PHASE2_TOOL_MODEL_FILE']},'historical_readers':{k:{'path':e[k],'sha256':sha(e[k])} for k in pins},'binary_sha256':sha(R/'target/debug/kanesys'),'source_file_map':str(a.source_map.resolve()),'full_source_freeze':str(a.full_source_freeze.resolve()),'full_source_freeze_sha256':sha(a.full_source_freeze),'raw_temp':str(a.temp),'skips_not_pass':True}
cmd=[str(R/'apps/api/.venv/bin/python'),'-m','pytest','-q','-rs','apps/api/tests','--basetemp',str(a.temp),'--junitxml',str(o/'pytest.xml')];record['command']=cmd;(o/'result.json').write_text(json.dumps(record,indent=2))
start=time.monotonic()
with (o/'pytest.log').open('wb') as log:r=subprocess.run(cmd,cwd=R,env=e,stdout=log,stderr=subprocess.STDOUT)
source_unchanged=all(sha(R/n)==h for n,h in full_freeze.items())
record['source_unchanged_after_run']=source_unchanged
if not source_unchanged and r.returncode==0:r.returncode=1
record.update(status='PASS' if r.returncode==0 else 'FAIL',exit_code=r.returncode,seconds=time.monotonic()-start)
import xml.etree.ElementTree as ET
if (o/'pytest.xml').exists():
 root=ET.parse(o/'pytest.xml').getroot();raw_cases=list(root.iter('testcase'));cases=[t for t in raw_cases if t.get('name') and t.get('classname')];record['junit_auxiliary_records_excluded']=len(raw_cases)-len(cases);record['interrupted']=r.returncode==2;record['counts']={'passed':sum(not any(c.tag in ['skipped','failure','error'] for c in t) for t in cases),'skipped':sum(t.find('skipped') is not None for t in cases),'failed':sum(t.find('failure') is not None for t in cases),'errors':sum(t.find('error') is not None for t in cases)}
 (o/'skips.json').write_text(json.dumps([{'class':t.get('classname'),'name':t.get('name'),'reason':t.find('skipped').get('message')} for t in cases if t.find('skipped') is not None],indent=2))
(o/'result.json').write_text(json.dumps(record,indent=2)+'\n');print(json.dumps(record));raise SystemExit(r.returncode)
