#!/usr/bin/python3
"""Owner-authorized reference fixture setup; not an Agent tool or production API."""
import json,os,re,socket,sys
from pathlib import Path
assert os.geteuid()==0
identity=sys.argv[1];assert re.fullmatch('[A-Za-z0-9-]{1,100}',identity)
config=json.loads((Path('/etc/kane-system/runtimes')/(identity+'.json')).read_text())
assert config['agent_id']==identity
base=Path('/run/kane-gateways');base.mkdir(mode=0o700,exist_ok=True);os.chown(base,901,901)
gate=base/identity;gate.mkdir(mode=0o700,exist_ok=True);os.chown(gate,901,901)
source=Path('/run/kane-system/model.sock');assert source.is_socket()
target=gate/'model.sock'
if target.exists():target.unlink()
os.link(source,target)
