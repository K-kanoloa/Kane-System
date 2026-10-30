#!/usr/bin/python3
"""Prepare systemd's real delegated cgroup, then exec the existing Rust manager."""
import json,os,re,sys
from pathlib import Path
identity=sys.argv[1]
if not re.fullmatch('[a-zA-Z0-9-]{1,100}',identity):raise SystemExit('invalid ModelInstance ID')
source=Path('/etc/kane-system/models')/(identity+'.json')
if source.is_symlink() or source.stat().st_uid!=0 or source.stat().st_mode&0o022:raise SystemExit('root-owned model spec required')
spec=json.loads(source.read_text());assert spec['instance_id']==identity
# A versioned reference-service default lives in this OS slot, not in shared
# Owner configuration. Rolling back retains readable Owner specs and the older
# slot's own default. Explicit Owner values remain authoritative and bounded by
# the original Rust manager's validation; real health/endpoint checks are intact.
policy=json.loads(Path('/usr/share/kane-system/reference-policy.json').read_text())
if 'health_timeout_seconds' not in spec:
 spec['health_timeout_seconds']=policy.get('model_loading_budget_seconds',45)
line=next(x for x in Path('/proc/self/cgroup').read_text().splitlines() if x.startswith('0::'))
root=Path('/sys/fs/cgroup')/line[3:].lstrip('/')
if root.name=='manager':
 assert root.parent.name=='kane-model@'+identity+'.service'
 root=root.parent
control=root/'manager';control.mkdir(exist_ok=True);(control/'cgroup.procs').write_text(str(os.getpid()))
(root/'cgroup.subtree_control').write_text('+cpu +memory +pids')
models=root/'models';models.mkdir();(models/'cgroup.subtree_control').write_text('+cpu +memory +pids')
spec['enforcement']={'mode':'cgroup_v2','root':str(models)}
out=Path('/run')/('kane-model-'+identity)/'effective.json'
with out.open('x') as f:os.fchmod(f.fileno(),0o600);json.dump(spec,f)
os.execve('/usr/bin/kanesys',['kanesys','--config','/etc/kane-system/core.env','model-run',str(out)],{'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':'/var/lib/kane-models'})
