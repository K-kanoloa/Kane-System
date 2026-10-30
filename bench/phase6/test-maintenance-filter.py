#!/usr/bin/env python3
"""Real tar extraction permissions and hostile path/link rejection regression."""
import importlib.util,io,tarfile,tempfile,pathlib,unittest
spec=importlib.util.spec_from_file_location('maintenance',pathlib.Path(__file__).resolve().parents[2]/'deploy/native/maintenance.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Extraction(unittest.TestCase):
 def test_permissions_and_content(self):
  buf=io.BytesIO()
  with tarfile.open(fileobj=buf,mode='w') as t:
   for mode in [0o400,0o600,0o700,0o755,0o4755]:
    h=tarfile.TarInfo(str(mode));h.mode=mode;h.size=7;t.addfile(h,io.BytesIO(b'payload'))
  buf.seek(0)
  with tempfile.TemporaryDirectory() as d:
   with tarfile.open(fileobj=buf) as t:t.extractall(d,filter=m.checkpoint_data_filter)
   for mode in [0o400,0o600,0o700,0o755,0o4755]:
    p=pathlib.Path(d)/str(mode);self.assertEqual(p.read_bytes(),b'payload');self.assertEqual(p.stat().st_mode&0o7777,mode&0o700)
 def test_escape_and_special_files_refused(self):
  with tempfile.TemporaryDirectory() as d:
   for name,kind,target in [('../escape',tarfile.REGTYPE,''),('link',tarfile.SYMTYPE,'/etc/shadow'),('device',tarfile.CHRTYPE,'')]:
    h=tarfile.TarInfo(name);h.type=kind;h.linkname=target
    with self.assertRaises(tarfile.FilterError):m.checkpoint_data_filter(h,d)
if __name__=='__main__':unittest.main()
