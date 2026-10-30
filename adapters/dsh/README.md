# DSH model observation adapter

`model-observer.mjs` is an optional first-party observation extension for the
existing DSH `llm/stream` boundary. It leaves the Agent Loop, provider adapters
and tools intact. It emits newline-delimited ACP extension notifications named
`kanesys/model_observation`; the supervised Kanaloa Adapter records them through
the private Rust Core socket. The extension receives no Core database or socket
credential.

Add it through the existing `--patch` mechanism:

```yaml
- insert:
    - id: kane-model-observer
      name: /absolute/path/to/adapters/dsh/model-observer.mjs
```

Each call has a UUID, native session reference, provider/model, start and result.
The result includes outcome, duration and provider-reported usage, or null when
usage is unavailable. Messages, prompts and credentials are not emitted.
ACP `usage_update` remains context occupancy; it must not be summed as billing
usage. This is observation, not an OS permission check or budget reservation.
Provider retries and child/auxiliary session coverage depend on the native
adapter boundaries; PR-2 verifies only the mapped session in the deterministic
fixture, not every external provider or delegated agent.

The test-only `bench/pr2/mock-model.mjs` registers a deterministic provider in
DSH's existing model interface. It uses the existing `read` tool and guards test
tool execution to `input.txt`, rejecting other tools/paths. Its reported tokens
are synthetic fixture values. It does not introduce an Agent or tool runtime.

Run `scripts/validate-system-execution.sh`. This uses isolated generated HOME
and workspace directories, no model credentials, no model network requests and
no production tools. Supervised PR-2 tasks admit one ACP prompt per Task; native
DSH can make multiple model/tool steps within it. Kanaloa's separate outer Loop
Mode and native steer are explicitly rejected in this mode pending a subsequent
Task admission contract. Standalone Kanaloa retains those behaviors. The legacy
capability descriptor describes underlying ACP support, not complete system
admission support; consumers must handle these explicit rejections.

A supervised Task without complete model observations cannot be accepted into
Verifying by the PR-2 completion path. Interrupted or ambiguous execution remains
Blocked(reconciliation), and restarting the process does not replay its prompt.
This adapter does not enable production remote-model use: data egress policy,
resource arbitration and tool enforcement still require their own acceptance.

## PR-3A protected model calls

Supervised execution now also requires `model-budget.mjs`. The observation
extension alone is insufficient; missing budget hooks are refused before prompt
submission. Standalone mode is unchanged. Add both hooks to the existing runtime
patch, with an absolute private path matching `<supervisor_socket>.budget.sock`:

```yaml
- insert:
    - id: kane-model-observer
      name: /absolute/path/to/adapters/dsh/model-observer.mjs
    - id: kane-model-budget
      name: /absolute/path/to/adapters/dsh/model-budget.mjs
      config:
        socket: /private/runtime/agent-ID.runtime.sock.budget.sock
```

The host ExecutionBridge creates this narrow relay before protected execution.
It must use the same Core socket and admitted Task as the Adapter. An existing
relay pathname is refused rather than unlinked; after an unclean host exit,
inspect and stop the previous owner before removing its stale socket. The runtime
receives no database handle or arbitrary Owner RPC through this endpoint.

The registered Model must include an Owner-reviewed `budget_profile`:

```json
{
  "budget_profile": {
    "provider": "kane-fixture",
    "model": "deterministic-v1",
    "upper_bound_tokens": 32,
    "bound_evidence": "deterministic fixture: at most 27 total synthetic tokens per call"
  }
}
```

This example is **only** for the test provider. A real provider needs separately
validated bounds including its input/output accounting and any hidden internal
retries; do not copy the fixture cap into a real deployment. No new provider is
implicitly approved by an evidence string. The hook does not mutate DSH's frozen
request or substitute a new model client. It reserves the backend's entire
approved per-call upper bound, rather than guessing input tokens from text length.

A permit must be received before `next()` enters the provider. Each native model
call and any retry that traverses this seam requests its own permit. Final clean
usage settles once; unknown usage keeps the reservation. Error finish chunks can
represent converted exceptions and do not prove partial usage is final. An overrun
is recorded in full and freezes the Budget. No automatic permit retry, refund or
fallback is performed. Child/auxiliary sessions without an admitted Task are
refused; their broader integration remains unverified.

Use `scripts/validate-model-budget.sh` for the native deterministic scenarios.
Query Core with `{"op":"model_budget","id":"<budget ID>"}` through the existing
trusted host socket to inspect aggregate usage and held tokens. Per-call records
and receipts are linked from `model_reserved` / `model_settled` Ledger events.
The policy and persistence live in Rust; the Python relay is not a second budget
implementation. This same-UID setup does not prevent malicious runtime code from
bypassing the hook or directly accessing host resources. Sandbox network, file
and process enforcement remains separate work. See ADR-0006 for migration and
accounting boundaries.

## PR-3B managed tool control (limited test profile)

Supervised prompts now require **both** `model-budget.mjs` and `tool-control.mjs`,
configured with the same private relay socket `<runtime socket>.budget.sock`.
Standalone Kanaloa is unchanged. Missing tool hook fails before prompt dispatch;
there is no observation-only fallback. The original DSH `tools/execute` extension
wraps the original tool body; it does not replace read/write or the Agent Loop.

The trusted Owner host operation `grant_tool` takes system Agent `id`, `tool`,
exact `arguments` object and Unix-seconds `expires_at`. Rust derives the scoped
Capability target. `revoke_capability` takes the returned Capability `id`.
The narrow runtime relay has no grant/revoke/Owner API. It exposes only model
reserve/settle and tool_prepare/tool_claim/tool_result for admitted native turns.

Only the fixed isolated read/write fixtures in `scripts/validate-tool-control.sh`
have been tested. Do not use this profile for unrestricted production tools.
This is a cooperative, same-UID host proxy, **not Linux sandbox enforcement**.
A successful tool receipt is execution evidence, not independent task acceptance.
Unknown outcomes require Owner evidence via `reconcile_action`; no automatic retry.
See ADR-0007.

## PR-4 optional container reference profile

Rust `supervise` accepts an optional `sandbox` object: pinned local image ID,
`cpu_millis`, `memory_bytes`, `pids`, explicit canonical `read_only` runtime paths,
and a dedicated `gateway_dir`. The memory limit must fit the registered Task.
There is no fallback to a host process when requested container startup fails.

The runtime workspace is read-only. Runtime home is separately writable and
persistent; without PR-4B capacity admission it remains unbounded. Docker networking is disabled.
The host adapter's optional `execution_socket` must equal
`<gateway_dir>/gateway.sock`, and both DSH hooks must announce that same path.
Only this dedicated directory is exposed, never the parent Core/Owner directory.

The reference test mounts only selected existing Node/DSH/adapter files, executes
original read tools and stores returned Markdown through the existing Core API.
PR-4B adds the restricted host write provider described below; do not remove the
read-only mount to bypass that boundary. No real model endpoint is enabled by
this test profile. `scripts/validate-sandbox-runtime.sh` runs the actual Docker
and cgroups tests. See ADR-0008.

### PR-4B restricted native file provider

With the trusted Supervisor Spec `sandbox.native_file_proxy: true` and the same
explicit hook option, the original DSH `write` tool delegates only `fs.writeText`
to Core. Core requires the existing exact tool grant, a workspace modify grant,
and a current edit lease. The workspace container mount remains read-only.
Only new flat UTF-8 files up to 4096 bytes are supported. Existing files, including
symlinks, are never replaced. Missing permits/Core or stale leases fail closed.
This is not general edit/overwrite support. See ADR-0009.

Optional `sandbox.home_capacity_bytes` requires an already mounted private,
Owner-owned dedicated ext4 filesystem at `runtime_home`. `kanesys storage-check
--path <absolute-mount> --max-bytes <limit>` probes the same admission rule;
it does not format or mount disks. A normal directory is rejected when this
option is requested. Omitting the field keeps the earlier experimental,
unbounded runtime-home profile. VM capacity evidence does not imply the
container fixture has been moved onto a bounded filesystem.

### PR-4C bounded-home integration fixture

`scripts/run-storage-runtime-vm.py` runs the original Kanaloa container fixture
inside a disposable offline Ubuntu VM. It copies local test dependencies and
starts a VM-local Docker daemon; it never mounts or formats a host block device.
Each scenario gets a fresh 64 MiB ext4 filesystem, admitted by the Rust
`home_capacity_bytes` check, with a 128 MiB Task storage ceiling.

The opt-in `test_storage_runtime.py` scenarios cover session restore without
prompt replay, and actual ENOSPC after a model permit has been reserved. A
fixture-owned fill file is removed explicitly before the recovery check.
This is a test harness, not a production disk provisioner or automatic recovery
policy. See the PR-4C report for actual outcomes and remaining gaps.

### PR-4 host-pool reference profile

`sandbox.host_resources: true` additionally requires an Owner-configured durable
host pool and a bounded HOME. Rust admits CPU, memory, PIDs and storage together
before container startup; uncertainty retains the claim. Stopping execution
releases volatile resources after confirmed cleanup, while persistent storage
remains reserved. The model/tool relay exposes no pool configuration or release
operations. See the reference profile and
ADR-0010 for setup, generation
fencing, daemon/host recovery, schema compatibility and precise boundaries.
