# Kane Connector Protocol v0.1 — public usage

This is an integration guide for the existing Agent-side Connector interface.
It contains no internal system blueprint or acceptance records.

Use a compatible Connector to connect an Agent to the running API. Configure the
API URL and stable Agent identity, pair through an authorized client, and keep
reconnect credentials in a protected local store. Use HTTPS/WSS for remote
connections; localhost HTTP/WS is for development only.

- [Codex Connector](codex/README.md): existing app-server integration.
- [MCP bootstrap](kane-mcp/README.md): optional discovery and connection checks.
- [Capability schema](capabilities.schema.json): the shared executable JSON
  contract used by the conformance checker and MCP discovery.

Connectors declare only capabilities their native Agent actually supports. The
existing wire envelope uses `protocol: kane-connector`, `version: 0.1`, an `id`,
a `type` and a `payload`. Start with `connector.hello`, wait for
`connector.ready`, and preserve Agent and native-session identity across
reconnections. Delivery acknowledgement is not task completion. Never replay an
uncertain operation merely because a transport connection was lost.

See the existing Connector implementation and conformance tests for message
fields. The optional MCP server reads this guide and the schema file directly;
it does not need private engineering documents. Its checks establish connection
and protocol behavior, not universal native Agent compatibility.

```sh
python3 -m unittest discover -s connectors/conformance -p 'test_*.py'
python3 -m unittest discover -s connectors/kane-mcp -p 'test_*.py'
```

Never include tokens, pairing codes, provider keys or private traces in reports.
