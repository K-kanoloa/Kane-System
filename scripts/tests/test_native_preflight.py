"""Capacity failures must stop controllers before creating data or starting VMs."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import native_preflight as preflight


class CapacityAdmission(unittest.TestCase):
    def probe(self, path, _mountinfo):
        return {"path": str(path), "device": 1, "filesystem": "ext4",
                "free_bytes": 16 * 1024**3, "read_only": False,
                "free_inodes": 100000, "total_inodes": 200000}

    def report(self):
        return preflight.capacity_report(output=Path('/output'), scratch=Path('/scratch'), workspace=Path('/source'))

    def test_shared_capacity_is_not_added_per_path(self):
        with patch.object(preflight, '_docker_root', return_value=Path('/docker')), \
                patch.object(preflight, '_probe', side_effect=self.probe):
            result = self.report()
        self.assertEqual(result['status'], 'PASS_PREFLIGHT_ONLY')
        self.assertEqual(result['filesystems_observed'], 1)
        self.assertFalse(result['phase6_closed'])

    def test_every_required_path_is_checked_separately(self):
        cases = [({'free_bytes': preflight.MIN_FREE_BYTES - 1}, 'insufficient_free_bytes'),
                 ({'filesystem': 'tmpfs'}, 'volatile_storage'),
                 ({'filesystem': 'ramfs'}, 'volatile_storage'),
                 ({'read_only': True}, 'read_only_storage'),
                 ({'free_inodes': 0}, 'inode_exhausted')]
        for bad_path in ['/source', '/output', '/scratch', '/docker']:
            for changes, code in cases:
                with self.subTest(path=bad_path, code=code):
                    def probe(path, info):
                        row = self.probe(path, info)
                        if str(path) == bad_path:
                            row.update(changes)
                        return row
                    with patch.object(preflight, '_docker_root', return_value=Path('/docker')), \
                            patch.object(preflight, '_probe', side_effect=probe):
                        result = self.report()
                    self.assertEqual(result['status'], 'BLOCKED')
                    self.assertTrue(any(r['code'] == code for r in result['reasons']))

    def test_unobservable_docker_fails_closed_without_secret_error_text(self):
        with patch.object(preflight, '_docker_root', side_effect=OSError('PRIVATE_DETAIL')), \
                patch.object(preflight, '_probe', side_effect=self.probe):
            result = self.report()
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertNotIn('PRIVATE_DETAIL', json.dumps(result))

    def test_docker_probe_is_pinned_local_and_bounded(self):
        with patch.dict(os.environ, {'DOCKER_HOST': 'tcp://unapproved.example:2375', 'DOCKER_CONTEXT': 'other'}), \
                patch.object(preflight.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '"/"\n')) as run:
            self.assertEqual(preflight._docker_root(), Path('/'))
        args, kwargs = run.call_args
        self.assertEqual(args[0], ['docker', '--host=unix:///var/run/docker.sock', 'info', '--format', '{{json .DockerRootDir}}'])
        self.assertEqual(kwargs['timeout'], 10)
        self.assertNotIn('DOCKER_HOST', kwargs['env'])
        self.assertNotIn('DOCKER_CONTEXT', kwargs['env'])

    def test_mount_matching_uses_component_boundary_and_decodes_spaces(self):
        mounts = ('1 0 0:1 / / rw - overlay overlay rw\n'
                  '2 1 0:2 / /tmp rw - tmpfs none rw\n'
                  '3 1 0:3 / /data\\040volume rw - ext4 /dev/test rw\n')
        self.assertEqual(preflight._mount_for(Path('/tmp/result'), mounts), ('/tmp', 'tmpfs'))
        self.assertEqual(preflight._mount_for(Path('/tmp-other'), mounts), ('/', 'overlay'))
        self.assertEqual(preflight._mount_for(Path('/data volume/result'), mounts), ('/data volume', 'ext4'))

    def test_real_probe_resolves_missing_output_without_creating_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / 'not-created' / 'result'
            alias = root / 'alias'
            alias.symlink_to(root, target_is_directory=True)
            row = preflight._probe(alias / 'not-created' / 'result', Path('/proc/self/mountinfo').read_text())
            self.assertEqual(row['path'], str(output))
            self.assertEqual(row['existing_ancestor'], str(root))
            self.assertFalse(output.parent.exists())

    def test_real_probe_rejects_file_in_place_of_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / 'file'
            file.write_text('unchanged')
            with self.assertRaises(ValueError):
                preflight._probe(file, Path('/proc/self/mountinfo').read_text())
            self.assertEqual(file.read_text(), 'unchanged')

    def test_all_heavy_entrypoints_refuse_before_output_or_vm_changes(self):
        scenarios = {
            'build-native-os.py': ['base', 'binary', 'debs', 'output', 'scratch', 'signing-key', 'public-key'],
            'validate-native-os.py': ['installer', 'output', 'scratch'],
            'exercise-native-system.py': ['acceptance', 'installer', 'scratch', 'assets', 'output'],
            'exercise-native-backup.py': ['acceptance', 'installer', 'scratch', 'output'],
            'exercise-native-restore.py': ['acceptance', 'installer', 'scratch', 'backup', 'output', 'source-acceptance'],
            'exercise-native-powercut.py': ['acceptance', 'installer', 'scratch', 'assets', 'output'],
            'exercise-native-upgrade.py': ['acceptance', 'installer', 'scratch', 'assets', 'output'],
            'run-native-python-regression.py': ['output', 'temp', 'source-map', 'full-source-freeze', 'fixtures'],
        }
        for script, flags in scenarios.items():
            with self.subTest(script=script), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                sentinel = root / 'old-evidence'
                sentinel.write_bytes(b'preserve original evidence')
                argv = [str(SCRIPTS / script)]
                for flag in flags:
                    argv += ['--' + flag, str(root / flag)]
                if script == 'build-native-os.py':
                    argv += ['--source-date-epoch', '1']
                denied = {'status': 'BLOCKED', 'phase6_closed': False, 'reasons': [{'code': 'insufficient_free_bytes'}]}
                with patch.object(sys, 'argv', argv), \
                        patch.object(preflight, 'capacity_report', return_value=denied) as check, \
                        patch('subprocess.run') as run, patch('subprocess.Popen') as popen, \
                        patch('pexpect.spawn') as spawn, contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as exit_info:
                        runpy.run_path(str(SCRIPTS / script), run_name='__main__')
                self.assertEqual(exit_info.exception.code, 2)
                check.assert_called_once()
                run.assert_not_called()
                popen.assert_not_called()
                spawn.assert_not_called()
                self.assertEqual(list(root.iterdir()), [sentinel])
                self.assertEqual(sentinel.read_bytes(), b'preserve original evidence')


if __name__ == '__main__':
    unittest.main()
