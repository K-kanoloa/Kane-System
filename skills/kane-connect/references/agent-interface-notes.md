# Agent interface checks

Use the Agent's documented native interface and verify the installed version.
Do not infer compatibility from a product name. Keep credentials on the Agent
host and disclose only the capabilities supported by that installation.

Preserve stable identity and native-session references after reconnecting.
Accepted delivery is not completion; wait for an explicit native completion
signal. Session loss does not prove that an external action was rolled back.
Do not automatically repeat uncertain work.

The public connection entry point is `connectors/README.md`. Validate the wire
behavior with the shared conformance checks, and test native execution separately.
