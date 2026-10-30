#!/usr/bin/env python3
"""Freeze current review source as a Git tree and check it out without a commit.

Preserves the project index, refs, worktree and history. Uses a separate object
store, includes existing tracked/untracked public source, excludes ignored state.
The resulting tree pack is self-contained; dependency caches are separate.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    source = out / 'source'
    checkout = out / 'checkout'
    source.mkdir()
    checkout.mkdir()
    names = sorted(set(p.decode() for p in subprocess.check_output(
        ['git', 'ls-files', '-co', '--exclude-standard', '-z'], cwd=ROOT).split(b'\0') if p))
    manifest = {}
    for name in names:
        p = ROOT / name
        if not p.is_file():
            continue
        if p.is_symlink():
            raise RuntimeError(f'Explicit source symlink review required: {name}')
        q = source / name
        q.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, q)
        manifest[name] = hashlib.sha256(q.read_bytes()).hexdigest()
    subprocess.run(['git', 'init', '--quiet'], cwd=source, check=True)
    subprocess.run(['git', 'add', '--force', '--all'], cwd=source, check=True)
    tree = subprocess.check_output(['git', 'write-tree'], cwd=source, text=True).strip()
    pack = out / 'source.pack'
    with pack.open('wb') as stream:
        subprocess.run(['git', 'pack-objects', '--stdout', '--revs'], input=(tree+'\n').encode(),
                       cwd=source, stdout=stream, check=True)
    subprocess.run(['git', 'init', '--quiet'], cwd=checkout, check=True)
    with pack.open('rb') as stream:
        subprocess.run(['git', 'index-pack', '--stdin'], cwd=checkout, stdin=stream, stdout=subprocess.DEVNULL, check=True)
    subprocess.run(['git', 'read-tree', tree], cwd=checkout, check=True)
    subprocess.run(['git', 'checkout-index', '--all'], cwd=checkout, check=True)
    subprocess.run(['git', 'diff', '--exit-code', tree], cwd=checkout, check=True)
    for name, digest in manifest.items():
        assert hashlib.sha256((checkout / name).read_bytes()).hexdigest() == digest, name
    if (ROOT / 'node_modules').is_dir():
        subprocess.run(['cp', '-al', str(ROOT / 'node_modules'), str(checkout / 'node_modules')], check=True)
        for link in (checkout / 'node_modules/@kane').iterdir():
            if link.is_symlink():
                assert link.resolve().is_relative_to(checkout), link
    assert not (checkout / 'target').exists()
    assert not subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard'], cwd=checkout)
    (out / 'source-files.json').write_text(json.dumps(manifest, indent=2))
    provenance = {'original_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                  'tree': tree, 'pack_sha256': hashlib.sha256(pack.read_bytes()).hexdigest(),
                  'source_manifest_sha256': hashlib.sha256((out / 'source-files.json').read_bytes()).hexdigest(),
                  'files': len(manifest), 'checkout': str(checkout), 'created_commits': 0,
                  'baseline': 'immutable Git tree, index and bytes all match; no HEAD commit created',
                  'dependencies': 'existing immutable hardlinked Node packages; workspace links resolve inside checkout; separate Python and Cargo caches'}
    (out / 'provenance.json').write_text(json.dumps(provenance, indent=2))
    print(json.dumps(provenance, indent=2))


if __name__ == '__main__':
    main()
