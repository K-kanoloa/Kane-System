#!/usr/bin/python3
"""Owner-only read-only native reference kernel evidence; no quota modifications."""
import json,os,subprocess,sys
from pathlib import Path
assert os.geteuid()==0
job=json.loads(Path(sys.argv[1]).read_text());rows={}
for kind,identity in [('model',job['instance']),('agent',job['agent'])]:
 unit='kane-'+kind+'@'+identity+'.service'
 values=subprocess.check_output(['systemctl','show',unit,'--property=MainPID,ControlGroup,ActiveState,SubState,Delegate,DelegateSubgroup'],text=True)
 entry={'systemd':values,'cgroups':{}}
 fields=dict(x.split('=',1) for x in values.splitlines() if '=' in x);base=Path('/sys/fs/cgroup')/fields.get('ControlGroup','').lstrip('/')
 entry['cgroup_present']=bool(fields.get('ControlGroup')) and base.exists()
 if entry['cgroup_present']:
  for directory,dirs,files in os.walk(base):
   p=Path(directory);observed={}
   for name in ['cgroup.procs','cpu.max','cpu.stat','memory.max','memory.current','memory.events','pids.max','pids.current']:
    f=p/name
    if f.is_file():observed[name]=f.read_text()
   entry['cgroups'][str(p)]=observed
 rows[kind]=entry
rows['host_meminfo']=Path('/proc/meminfo').read_text();rows['mounts']=subprocess.check_output(['findmnt','-rn','-o','TARGET,SOURCE,FSTYPE,OPTIONS'],text=True)
rows['containers']=subprocess.check_output(['/usr/local/bin/docker','ps','--no-trunc','--format','{{json .}}'],text=True)
rows['container_kernel_limits']={}
for line in rows['containers'].splitlines():
 container=json.loads(line);identity=container.get('ID')
 if not identity:continue
 pid=int(subprocess.check_output(['/usr/local/bin/docker','inspect','--format','{{.State.Pid}}',identity],text=True))
 if pid<=0:continue
 hierarchy=next(x[3:] for x in Path('/proc',str(pid),'cgroup').read_text().splitlines() if x.startswith('0::'))
 group=Path('/sys/fs/cgroup')/hierarchy.lstrip('/')
 rows['container_kernel_limits'][identity]={'pid':pid,'cgroup':str(group),'values':{n:(group/n).read_text() for n in ['cpu.max','cpu.stat','memory.max','memory.current','memory.events','pids.max','pids.current']}}
print(json.dumps(rows,indent=2))
