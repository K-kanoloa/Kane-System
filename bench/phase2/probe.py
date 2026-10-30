#!/usr/bin/env python3
"""Record actual host/controller capacities; no accelerator support inference."""
import json,os,platform,shutil,subprocess
from pathlib import Path
cgroup=Path('/sys/fs/cgroup')
def read(path):
 try:return path.read_text().strip()
 except OSError:return None
cpu_max=read(cgroup/'cpu.max');quota,period=cpu_max.split() if cpu_max else ('max','100000')
mem={k:int(v.split()[0])*1024 for k,v in (line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines())}
limit=read(cgroup/'memory.max');cpu_millis=min(len(os.sched_getaffinity(0))*1000,int(quota)*1000//int(period) if quota!='max' else 2**63-1)
result={'arch':platform.machine(),'kernel':platform.release(),'os_release':Path('/etc/os-release').read_text(),'cpu':{'affinity':len(os.sched_getaffinity(0)),'cgroup_cpu_max':cpu_max,'effective_millis':cpu_millis},'memory':{'physical_bytes':mem['MemTotal'],'available_bytes':mem['MemAvailable'],'cgroup_limit':limit,'effective_total_bytes':min(mem['MemTotal'],int(limit) if limit and limit!='max' else mem['MemTotal']),'layout':'CPU-only; accelerator/shared-memory layout UNKNOWN'},'cgroup':{f:read(cgroup/f) for f in ['cgroup.controllers','cpu.stat','memory.current','memory.events','pids.max','pids.current']},'storage':{p:{'total':shutil.disk_usage(p).total,'free':shutil.disk_usage(p).free} for p in ['/workspace','/tmp','/dev/shm']},'accelerator_verified':False,'docker':json.loads(subprocess.check_output(['docker','info','--format','{{json .}}']))}
# Exclude host labels/names and environment configuration from the Docker record.
result['docker']={k:result['docker'].get(k) for k in ['ServerVersion','Driver','CgroupVersion','CgroupDriver','NCPU','MemTotal']}
print(json.dumps(result,indent=2))
