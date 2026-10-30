"""Exercise installer refusal paths without root, real accounts or real disks."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace as Row
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('native_guest', ROOT/'scripts/build-native-os-guest.py')
guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.groups = {}
        self.users = {}
        def get(rows, key, field):
            for row in rows.values():
                if getattr(row, field) == key:
                    return row
            raise KeyError(key)
        self.patches = [
            patch.object(guest.grp, 'getgrnam', side_effect=lambda x: get(self.groups,x,'gr_name')),
            patch.object(guest.grp, 'getgrgid', side_effect=lambda x: get(self.groups,x,'gr_gid')),
            patch.object(guest.pwd, 'getpwnam', side_effect=lambda x: get(self.users,x,'pw_name')),
            patch.object(guest.pwd, 'getpwuid', side_effect=lambda x: get(self.users,x,'pw_uid')),
        ]
        for p in self.patches:
            p.start();self.addCleanup(p.stop)
    def create(self, *args):
        if args[0] == 'groupadd':
            name=args[-1];self.groups[name]=Row(gr_name=name,gr_gid=int(args[3]))
        elif args[0] == 'useradd':
            self.users['kane-system']=Row(pw_name='kane-system',pw_uid=901,pw_gid=901,
                pw_dir='/var/lib/kane-system',pw_shell='/usr/sbin/nologin')
        else: self.fail('unexpected mutation')
    def test_fresh_creation_then_reentry_does_not_duplicate_or_renumber(self):
        with patch.object(guest,'run',side_effect=self.create) as run:
            guest.ensure_native_identity()
            self.assertEqual(run.call_count,3)
            guest.ensure_native_identity()
            self.assertEqual(run.call_count,3)
    def test_conflicts_fail_before_any_account_mutation(self):
        for kind in ['wrong_gid','occupied_gid','wrong_uid','occupied_uid','wrong_home']:
            with self.subTest(kind=kind):
                self.groups.clear();self.users.clear()
                if kind=='wrong_gid':self.groups['kane-system']=Row(gr_name='kane-system',gr_gid=988)
                if kind=='occupied_gid':self.groups['other']=Row(gr_name='other',gr_gid=902)
                if kind in ('wrong_uid','wrong_home'):
                    self.users['kane-system']=Row(pw_name='kane-system',pw_uid=988 if kind=='wrong_uid' else 901,
                        pw_gid=901,pw_dir='/wrong' if kind=='wrong_home' else '/var/lib/kane-system',pw_shell='/usr/sbin/nologin')
                if kind=='occupied_uid':self.users['other']=Row(pw_name='other',pw_uid=901)
                with patch.object(guest,'run') as run:
                    with self.assertRaisesRegex(RuntimeError,'identity conflict'):guest.ensure_native_identity()
                    run.assert_not_called()


class InstallerTests(unittest.TestCase):
    tools = 'cat id mkdir rmdir sfdisk udevadm mkfs.vfat mkfs.btrfs mount umount btrfs bsdtar blkid chmod chown cp find sort tail ln chroot grub-editenv sync'.split()
    def invoke(self, missing=None, archive_exit=0, check_only=False):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);log=p/'calls'
            for name in self.tools:
                if name==missing:continue
                file=p/name
                file.write_text(f'#!/bin/sh\nprintf "%s\\n" "{name}" >> "{log}"\nexit {archive_exit if name=="bsdtar" else 0}\n')
                file.chmod(0o755)
            args=['--check-dependencies'] if check_only else ['/unused-bundle','/unused-disk']
            result=subprocess.run(['/bin/sh',str(ROOT/'deploy/native/install-disk'),*args],
                env={**os.environ,'PATH':tmp},capture_output=True,text=True)
            return result,log.read_text().splitlines() if log.exists() else []
    def test_every_missing_dependency_prevents_all_disk_commands(self):
        for name in self.tools:
            with self.subTest(tool=name):
                result,calls=self.invoke(missing=name)
                self.assertNotEqual(result.returncode,0)
                self.assertIn('Missing installation dependency: '+name,result.stderr)
                self.assertEqual(calls,[])
    def test_archive_loader_failure_prevents_disk_commands(self):
        result,calls=self.invoke(archive_exit=127)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(calls,['bsdtar'])
    def test_dependency_only_check_never_touches_disk_or_accounts(self):
        result,calls=self.invoke(check_only=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(calls,['bsdtar'])


if __name__=='__main__':unittest.main()
