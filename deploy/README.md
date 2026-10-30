# Build & package

These are development tools, not a production installer download. Use an
authorized Linux machine and disposable virtual disks for native experiments.

## Core package

Requirements: the pinned Rust toolchain and C build tools, Python 3.11+,
`dpkg-deb` and Git. Build on a platform compatible with the package's declared
Linux dependencies.

```sh
cargo build --release --locked -p kane-supervisor
python3 scripts/build-system-package.py --binary target/release/kanesys
```

The script builds a `.deb` and checksum under `dist/system/`; it does not install
it and refuses to overwrite an existing package. The package includes the native
service units, default configuration and license. Inspect it with `dpkg-deb --info`
and `dpkg-deb --contents` before any installation.

## Native image tools

Host-side controllers require Docker, the tools image built from
`deploy/validation-tools.Dockerfile`, OpenSSL, and Python dependencies:

```sh
python3 -m venv .venv-native
.venv-native/bin/python -m pip install -r scripts/requirements-native-tests.txt
docker build -f deploy/validation-tools.Dockerfile -t kane-system-validation:20261018 .
.venv-native/bin/python scripts/build-native-os.py --help
.venv-native/bin/python scripts/validate-native-os.py --help
```

Provide an independently verified Linux base image, the compiled binary,
complete offline distribution packages (including `libarchive-tools` and its
matching dependencies for `bsdtar`), and your own signing materials. Agent and
model runtime archives are optional explicit inputs. Never commit signing keys,
Owner credentials, runtime archives, model weights or VM disks.

Storage preflight requires at least 10 GiB free on each relevant filesystem;
15 GiB or more is recommended. This is a minimum prerequisite, not a promise that
every workload fits. Use adequate RAM and CPU and inspect the CLI before running
any VM controller. A successful image build is not an installation/recovery test.

Local evidence archival requires an explicit private `--phase-state` file.
The opt-in Python regression controller requires a private `--fixtures` JSON
mapping each dependency name to its `path` and `sha256`. Its model inputs are
`KANE_LOCAL_MODEL_SERVER`, `KANE_LOCAL_MODEL_FILE` and `KANE_PHASE2_TOOL_MODEL_FILE`;
its historical reader inputs are `KANE_PHASE3_SCHEMA7_BINARY`,
`KANE_PREVIOUS_CORE_BINARY` and `KANE_PHASE5_SCHEMA8_BINARY`. Use actual compatible
historical readers; do not substitute the current binary to claim compatibility.
Without those inputs, use the ordinary test suite and report skipped integrations.
Private material stays outside the repository.
