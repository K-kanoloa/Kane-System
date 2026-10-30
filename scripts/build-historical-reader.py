#!/usr/bin/env python3
"""Build a schema reader from an explicitly selected local historical Git tree.

Source, binaries and provenance remain in a new local output directory. No
checkout/reset, source publication, production DB access or network Git fetch.
Cargo uses its existing offline cache; prepare dependencies separately.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile


def verify_reader(binary, schema):
    with tempfile.TemporaryDirectory(prefix='kane-reader-probe-') as tmp:
        db = Path(tmp) / 'probe.db'
        subprocess.run([str(binary), '--db', str(db), 'init'], check=True,
                       capture_output=True, timeout=30)
        if not db.is_file():
            raise RuntimeError('historical reader did not create its probe database')
        with sqlite3.connect(f'{db.as_uri()}?mode=ro', uri=True) as conn:
            actual = conn.execute('PRAGMA user_version').fetchone()[0]
        if actual != schema:
            raise RuntimeError(f'reader schema mismatch: expected {schema}, got {actual}')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--revision', required=True, help='Full local commit SHA')
    p.add_argument('--expected-tree', required=True, help='Independently recorded source tree SHA')
    p.add_argument('--schema', type=int, choices=[6, 7, 8], required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    for value in (a.revision, a.expected_tree):
        if not re.fullmatch(r'[0-9a-f]{40}', value):
            p.error('revision and expected-tree must be full hexadecimal SHA-1 IDs')
    def git(*args):
        return subprocess.check_output(['git', '-C', str(a.source.resolve()), *args])
    tree = git('rev-parse', a.revision + '^{tree}').decode().strip()
    if tree != a.expected_tree:
        p.error('historical source tree mismatch')
    out = a.output.resolve()
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    source = out / 'source'
    source.mkdir()
    names = []
    prefixes = ('core/', 'models/', 'supervisor/')
    top = {'Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml', 'LICENSE', 'THIRD_PARTY_NOTICES.md'}
    for entry in git('ls-tree', '-rz', a.revision).split(b'\0'):
        if not entry:
            continue
        meta, raw_name = entry.split(b'\t', 1)
        mode, kind, oid = meta.decode().split()
        name = raw_name.decode()
        if name not in top and not name.startswith(prefixes):
            continue
        if kind != 'blob' or mode not in ('100644', '100755') or '..' in Path(name).parts or Path(name).is_absolute():
            raise RuntimeError('unsupported historical source entry')
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(git('cat-file', 'blob', oid))
        target.chmod(0o755 if mode == '100755' else 0o644)
        names.append(name)
    if not {'Cargo.toml', 'Cargo.lock', 'core/Cargo.toml', 'supervisor/Cargo.toml'} <= set(names):
        raise RuntimeError('required historical Rust source missing')
    with (out / 'build.log').open('wb') as log:
        subprocess.run(['cargo', 'build', '--release', '--locked', '--offline', '-p', 'kane-supervisor',
                        '--target-dir', str(out / 'target')], cwd=source,
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    binary = out / 'target/release/kanesys'
    verify_reader(binary, a.schema)
    result = {'path': str(binary), 'sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
              'source_commit': a.revision, 'source_tree': tree, 'observed_schema': a.schema,
              'scope': 'reader build and empty database schema probe; migration suite still required'}
    (out / 'reader.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
