"""Package wiring only: /bin/true is an ELF fixture, never a working Kane service."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class PublicPackage(unittest.TestCase):
    @unittest.skipUnless(shutil.which('dpkg-deb') and Path('/bin/true').is_file(),
                         'requires Linux ELF fixture and dpkg-deb')
    def test_package_has_public_docs_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = [sys.executable, str(ROOT / 'scripts/build-system-package.py'),
                       '--binary', '/bin/true', '--output', tmp, '--version', '0.3.0~fixture1']
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            package = next(Path(tmp).glob('*.deb'))
            before = hashlib.sha256(package.read_bytes()).hexdigest()
            self.assertEqual(package.with_suffix('.deb.sha256').read_text().split()[0], before)
            extract = Path(tmp) / 'extract'
            subprocess.run(['dpkg-deb', '--extract', str(package), str(extract)], check=True)
            docs = extract / 'usr/share/doc/kane-system-core'
            self.assertEqual((docs / 'README.md').read_bytes(), (ROOT / 'deploy/README.md').read_bytes())
            self.assertEqual((docs / 'copyright').read_bytes(), (ROOT / 'LICENSE').read_bytes())
            self.assertFalse(json.loads((docs / 'build.json').read_text())['release'])
            retry = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(retry.returncode, 0)
            self.assertIn('refusing to overwrite', retry.stderr)
            self.assertEqual(hashlib.sha256(package.read_bytes()).hexdigest(), before)


if __name__ == '__main__':
    unittest.main()
