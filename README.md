<p align="center"><img src="apps/web/public/kane-octopus.png" width="112" alt="Kane System"></p>
<h1 align="center">Kane System</h1>
<p align="center"><strong>Agent-first Linux System</strong></p>
<p align="center">A home for AI work. Built on Linux. Controlled by you.</p>
<p align="center">
  <a href="#status"><img src="https://img.shields.io/badge/status-development_preview-orange" alt="Development preview"></a>
  <img src="https://img.shields.io/badge/core-Rust-dea584" alt="Rust core">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-PolyForm_Noncommercial_1.0.0-blue" alt="PolyForm Noncommercial 1.0.0"></a>
</p>
<p align="center">
  <a href="https://k-kanoloa.github.io/Kane-System/">Website</a> ·
  <a href="#why-kane-system">Why Kane System</a> ·
  <a href="#develop-from-source">Get started</a> ·
  <a href="https://github.com/K-kanoloa/Kane-System/issues">Feedback</a>
</p>

Kane System brings models, Agents and their work into a Linux environment you
control. Follow a task, inspect its results and step in when you need to.

The project combines a Rust system core with existing Linux services. Its intended
delivery is a bootable, installable Linux environment for AI work.

> **Development preview.** System installation, recovery and platform validation
> are still in progress. No production release or ready-to-use public installer
> is available. The commands below run the development project.

## Why Kane System

**More control over the work. Less switching between tools.**

- **Models and Agents, together.** Bring compatible model backends and Agent
  integrations into one working environment.
- **Your workspace stays useful.** Inspect tasks and work with supported editable
  files, rather than treating every result as a chat message.
- **Stay in control.** Review access, resource use and results; take over supported
  workspaces when a task needs your attention.
- **Results you can check.** Separate generated output from accepted work.
- **A Linux foundation.** Build on familiar system tooling, with source available
  for inspection and development.

Available behavior depends on the configured backend, execution profile and
platform. Compatibility with every model, Agent or device is not implied.

## Develop from source

Use a Linux development environment with **Node.js 24+**, **Python 3.11+**,
`python3-venv`, a C compiler and the **Rust 1.99.1** toolchain pinned in
[rust-toolchain.toml](rust-toolchain.toml). Include `rustfmt` and `clippy`.

```sh
git clone https://github.com/K-kanoloa/Kane-System.git
cd Kane-System

# Install the locked JS dependencies and the existing DSH integration patch.
npm ci

# Prepare both Python services.
python3 -m venv apps/api/.venv
apps/api/.venv/bin/python -m pip install -r apps/api/requirements-dev.txt
python3 -m venv apps/local-bridge/.venv
apps/local-bridge/.venv/bin/python -m pip install -r apps/local-bridge/requirements.txt

# Build the Rust system components.
cargo build --workspace --locked

# Start the local development services.
npm run dev
```

Open **http://127.0.0.1:3000**. Review [.env.example](.env.example) for local
configuration. Supply your own authorized model endpoint and credentials when
running model-backed tasks; model weights and credentials are not bundled.
Keep development services on localhost unless you have configured authentication
and encrypted access for remote use.

For package tooling and native build prerequisites, see [Build & package](deploy/README.md).
For optional Agent connections, see [Connector usage](connectors/README.md).

## Check your build

```sh
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --locked
npm run test:api
npm run test:bridge
npm run typecheck:web
npm run lint
npm run build:web
python3 scripts/check-public-links.py
```

Some Linux isolation tests require a non-root host user. Integration tests need
explicit fixtures, model backends, containers or disposable VMs. Skipped tests
are not passing tests; a green source check does not certify a complete OS install.

## Status

**v0.3 is in development.** Contributions and reproducible bug reports are welcome.
Use disposable environments for installation experiments and keep important data
backed up. Supported behavior must be verified on your chosen environment before
relying on it.

Please include the source version, environment and reproduction steps in
[bug reports](https://github.com/K-kanoloa/Kane-System/issues/new/choose).
Do not attach credentials, private files or unredacted runtime logs.

## License

**[PolyForm Noncommercial License 1.0.0](LICENSE).** Public source access does not
grant unrestricted commercial use. Integrated Kanaloa code retains its existing
noncommercial restrictions. Third-party components keep their respective licenses
and notices; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
