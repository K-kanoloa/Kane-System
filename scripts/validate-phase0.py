#!/usr/bin/env python3
"""Reproduce Phase 0 checks from an already clean, isolated source checkout.

Cached Cargo crates, Python environment and Node packages are allowed; no network,
provider credentials, commit to the project branch or VM mutation is performed.
Passing these commands does not close Phase 0: review the acceptance matrix too.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--source-tree', help='Immutable Git tree checkout; validates without creating a source commit')
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    def source_clean():
        if args.source_tree:
            return (subprocess.run(['git', 'diff', '--quiet', args.source_tree], cwd=ROOT).returncode == 0
                    and not subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard'], cwd=ROOT))
        return not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT)
    assert source_clean(), 'source checkout must match the immutable baseline'
    assert not (ROOT / 'target').exists(), 'fresh build output required'
    env = os.environ.copy()
    env.update(CARGO_INCREMENTAL='0', CARGO_PROFILE_DEV_DEBUG='0', CARGO_PROFILE_TEST_DEBUG='0',
               PYTHONPATH=str(ROOT / 'apps/api'))
    # All existing local deterministic integration suites, no real provider tests.
    for key in ['KANE_PR1_TEST', 'KANE_PR2_TEST', 'KANE_PR3A_TEST', 'KANE_PR3B_TEST', 'KANE_PR4_TEST', 'KANE_FIFO_TEST']:
        env[key] = '1'
    env.pop('KANE_PHASE0_TEST', None)
    results = []

    def run(label, command, custom_env=None):
        started = time.monotonic()
        with (out / f'{label}.log').open('wb') as log:
            result = subprocess.run(command, cwd=ROOT, env=custom_env or env, stdout=log, stderr=subprocess.STDOUT)
        results.append({'check': label, 'command': command, 'exit_code': result.returncode,
                        'seconds': time.monotonic() - started})
        (out / 'commands.json').write_text(json.dumps(results, indent=2))
        print(json.dumps(results[-1]), flush=True)
        return result.returncode == 0

    profile = {'date_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
               'snapshot_commit': None if args.source_tree else subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
               'snapshot_tree': args.source_tree,
               'platform': platform.platform(), 'arch': platform.machine(), 'python': sys.version,
               'dependencies': {name: importlib.metadata.version(name) for name in ['pytest', 'pytest-asyncio', 'httpx', 'fastapi']},
               'build': {'incremental': False, 'debug_info': False, 'fresh_target': True, 'offline': True},
               'model': 'deterministic-v1 scripted mock; no provider billing/quality', 'network': 'Agent network=none; local Unix IPC',
               'objective': {'correct_workflow': 1, 'speed_advantage': 0}}
    (out / 'profile.json').write_text(json.dumps(profile, indent=2))
    run('rust-version', ['rustc', '--version'])
    run('node-version', ['node', '--version'])
    run('python-source-path', [sys.executable, '-c', 'import app.system.client; print(app.system.client.__file__)'])
    run('docker-version', ['docker', 'version', '--format', '{{.Server.Version}}'])
    run('docker-image', ['docker', 'image', 'inspect', 'ubuntu:24.04', '--format', '{{.Id}}'])
    run('host-profile', ['sh', '-c', 'cat /etc/os-release /proc/cpuinfo /proc/meminfo /proc/self/cgroup; stat -fc %T /sys/fs/cgroup; cat /sys/fs/cgroup/cgroup.controllers; df -T .'])
    run('fmt', ['cargo', 'fmt', '--all', '--check'])
    built = run('build', ['cargo', 'build', '--workspace', '--locked', '--offline'])
    if not built:
        return 1
    run('doctor', [str(ROOT / 'target/debug/kanesys'), 'doctor'])
    run('rust-tests', ['cargo', 'test', '--workspace', '--locked', '--offline'])
    run('clippy', ['cargo', 'clippy', '--workspace', '--all-targets', '--locked', '--offline', '--', '-D', 'warnings'])
    # AF_UNIX addresses have a small kernel path limit. Evidence destinations
    # can be arbitrarily long, so run IPC fixtures under a short private path
    # and retain their outputs separately. Never weaken the service's checks.
    temporary = Path(tempfile.mkdtemp(prefix='k0-'))
    run('python-regression', [sys.executable, '-m', 'pytest', '-q', '-rs', 'apps/api/tests', '--basetemp', str(temporary / 'regression')])
    shutil.copytree(temporary / 'regression', out / 'regression-cases', symlinks=True,
                    ignore=shutil.ignore_patterns('*.sock', '*.sock.*', '*service-lock'))
    baseline_env = env | {'KANE_PHASE0_TEST': '1'}
    run('baseline', [sys.executable, '-m', 'pytest', '-q', '-rs', 'apps/api/tests/test_phase0_baseline.py', '--basetemp', str(temporary / 'baseline')], baseline_env)
    shutil.copytree(temporary / 'baseline', out / 'baseline-cases', symlinks=True,
                    ignore=shutil.ignore_patterns('*.sock', '*.sock.*', '*service-lock'))
    run('source-clean-after', ['git', 'diff', '--exit-code', args.source_tree] if args.source_tree else ['git', 'status', '--porcelain'])
    binary = ROOT / 'target/debug/kanesys'
    (out / 'binary-sha256.txt').write_text(hashlib.sha256(binary.read_bytes()).hexdigest() + '\n')
    samples = []
    for path in sorted((out / 'baseline-cases').rglob('baseline-result.json')):
        row = json.loads(path.read_text())
        row['evidence'] = str(path.relative_to(out))
        samples.append(row)
    suite = json.loads((ROOT / 'bench/phase0/tasks.json').read_text())
    summary = {'suite': suite, 'attempts': samples, 'tasks': {}}
    complete = True
    for task in suite['tasks']:
        rows = [row for row in samples if row['task'] == task['id']]
        passed = [row for row in rows if row['verified_success']]
        complete &= len(rows) == suite['runs_per_task'] == len(passed)
        summary['tasks'][task['id']] = {'attempts': len(rows), 'passed': len(passed),
                                        'failed': len(rows)-len(passed)}
        for metric in ['execution_ms', 'total_ms', 'verification_ms', 'synthetic_tokens']:
            values = [row[metric] for row in passed]
            if values:
                q1, _, q3 = statistics.quantiles(values, n=4, method='inclusive') if len(values)>1 else [values[0]]*3
                summary['tasks'][task['id']][metric] = {'median': statistics.median(values), 'q1': q1, 'q3': q3, 'iqr': q3-q1}
    summary['required_samples_present_and_passed'] = complete
    summary['phase0_closed'] = False
    summary['limitation'] = 'Shared Mock FIFO is tested separately in the full regression. Review all seven acceptance items; passing commands alone does not close Phase 0.'
    (out / 'baseline-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    clean = source_clean()
    return 0 if complete and clean and all(r['exit_code']==0 for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
