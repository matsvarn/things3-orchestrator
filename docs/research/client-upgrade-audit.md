Client upgrade audit, 2026-09-05

Audit baseline: v0.10.5, commit `517df9906d29092725b02b45dd780edd41029bfc`.
This is a design recommendation, not an implemented or deployed upgrade path.

The goal is for ordinary host upgrades to require no client maintenance. When
client action is necessary, the product should identify the affected component
and give one specific next step. Requiring every client to match every host
release would make the current coordination problem permanent.

The reported stale September 4 bridge process is plausible but unverified.
This audit inspected repository code and used synthetic local data. It did not
inspect the affected Grogbot runtime, its process tree, or its cached schemas.

| Finding | Evidence and consequence |
|---|---|
| Optional output additions can break old clients. | `StrictModel` in [v2.py](../../src/things_orchestrator/v2.py) forbids extra properties. Both `PublicItem` and `PublicResult` inherit it. The v0.10.5 `notes_state` field already has a default. An old schema still rejects its presence. |
| The failure is reproducible independently of Grogbot. | Load `v0.10.4:src/things_orchestrator/v2.py` as a separate Python module. Validate a synthetic v0.10.5 `_domain_result` against its flattened `PublicResult` schema with `Draft202012Validator`. It fails at `items[0]`: `Additional properties are not allowed ('notes_state' was unexpected)`. The current schema accepts it. Removing only that field makes the old schema accept it. |
| Existing hashes describe internal models, not the exact discovered contract. | [deployment.py](../../src/things_orchestrator/deployment.py) hashes `MODELS` and unflattened output schemas. [server.py](../../src/things_orchestrator/server.py) advertises flattened `DISCOVERY_MODELS`, including a different capture discovery model. A discovery-only change need not change the existing hash. |
| Doctor is a deployment check. | [doctor.py](../../src/things_orchestrator/doctor.py) compares fresh HTTP health and MCP initialization against the local package version, commit, and hashes. It retains tool names but discards actual schemas. It cannot prove an existing client refreshed its catalog or prompt. |
| Health cannot repair client state. | Authenticated health already reports version, commit when known, schema hash, and contract hash. Public health reports only liveness. The HTTP server is stateless and has no client installation or prompt inventory. |
| Instructions have separate installation paths. | [client_config.py](../../src/things_orchestrator/client_config.py) pins the Hermes skill URL to the locally installed package version. The URL targets a single `SKILL.md`, whose required references need separate installation verification. [routines.md](../routines.md) explicitly says server upgrades do not replace saved receiver prompts. |
| Same-host stdio is a separate server. | [plugin configuration](../../plugin/.mcp.json) starts `serve`, using the [local launcher](../../plugin/bin/things-orchestrator). It does not proxy the HTTP host. Updating a remote host cannot update this process or its bundled skill. |
| The recovery link misses the reported problem. | [clients.md](../clients.md) directs failed discovery to [recovery.md](../recovery.md), which documents operation and routine recovery, not stale connections or schemas. |

The reproduced rejection uses the real previous-release schema from tag
`v0.10.4`, commit `12689424bfda0631530f786f7a0aecbe5c95209f`. It establishes
the compatibility defect, not which process performed validation in the incident.
MCP explicitly permits output schemas and recommends client validation. See the
[MCP tools specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools).

The five proposed fixes need these adjustments:

| Proposal | Recommendation |
|---|---|
| Release client packet | Adopt. Generate one complete bundle from canonical skill files, required references, and receiver templates. Include immutable release identity, file checksums, and client-impact metadata. Reuse packaged assets and existing release checks. Keep credentials and personal prompt customizations out. |
| Optional additions or a compatibility projection | Fix output evolution first. Optionality does not address unknown-property rejection. Keep strict input validation and internal result construction. Make the published output schema tolerate additive properties at documented output objects. This requires an initial catalog refresh. A projection for old clients is unnecessary unless evidence establishes a migration requirement that cannot be met by refresh. Never hide incomplete-note semantics merely to pass an old validator. |
| New `things_client_check` tool or expanded health | Keep the eight task tools. Extend authenticated diagnostics with client-bundle identity and a hash of the actual advertised tools. Implement the comparison in a client-side check or installation helper. Host health alone cannot know the client's cache, installed skill, or saved prompt. |
| Kill bridge on schema-hash change | Defer as the default solution. Prefer native HTTP where supported. For a necessary stdio bridge, first prove who caches schemas and who restarts the process. Exit alone does not ensure rediscovery or restart. Do not kill based on process age or every host reboot. |
| Machine-readable client impact | Adopt inside the same bundle manifest. Distinguish required migration from an optional refresh. Identify changes to tools, the base skill, and individual routine templates separately. Avoid implementation-specific actions such as `expect_notes_state` as the universal upgrade API. |

The client bundle should describe the running host's release. It must not tell
a client to fetch GitHub's latest release when its host is older or rolled back.
An authenticated, fixed host endpoint for the installed bundle is the preferred
eventual fetch path. The release asset remains useful for manual and offline
installation. Reuse the same generated bytes for both delivery paths.

One exact tool fingerprint should come from the same definitions as `tools/list`,
covering advertised schemas, descriptions, and annotations. An exact fingerprint
detects change; it does not decide compatibility. Release policy must separately
classify removed fields, changed types, enum changes, required inputs, and changed
behavior. Do not require skill updates for unrelated server bug fixes.

Input strictness must remain intact. Output tolerance is not permission to emit
arbitrary fields or reinterpret unknown outcome states as success. Preserve the
server's [incomplete-note write protection](../../src/things_orchestrator/workspace.py)
and existing account fences, receipts, and retry rules. Older schemas remain
closed after a new server release, so the first migration needs an explicit
client catalog refresh. Future additive changes benefit from the new policy.

The deployment combinations reduce to connection ownership and runtime lifetime:

| Arrangement | Expected path and update responsibility |
|---|---|
| Desktop client and service on one Mac | Prefer the supervised HTTP service. Update and verify that service once. The client refreshes only when its catalog or instructions require it. Sleeping Macs remain an availability limitation for remote clients. |
| Client on another Mac, Windows machine, or Linux host | Use the serving host's HTTP endpoint and MCP bearer. The client does not need Things Cloud credentials or a local Things server installation. Network reachability still depends on where that client runs. |
| Agent and service on one always-on Linux host | Use HTTP to the existing service. Agent process restarts and service updates are separate operations. Containers have their own loopback interface, so configure a reachable service address. |
| Hosted Grok Custom connector | The existing Grok renderer targets the public HTTPS connector workflow. Provider-managed reconnect and prompt steps require a manual adapter and verification. Do not infer webhook Bot access from a successful interactive conversation. |
| Grogbot or another local agent using stdio-to-HTTP | Inspect the actual executable, transport, and process owner before applying Grok web instructions. `mcp-remote` is a bridge for clients needing local stdio. Prefer native HTTP if this particular client supports it. |
| Current same-host Codex plugin | This is a separately launched local server plus bundled skill. Its lifetime and upgrades are independent of the remote service. Retain it as an explicit advanced path, or simplify its supported role separately after checking plugin constraints. |
| Long-lived routine receiver | Refresh the selected saved routine template when required, preserving owner customization through review. A successful webhook delivery is insufficient. Verify the actual run has the connector and can read its selected task. |

The [mcp-remote project](https://github.com/punkpeye/mcp-remote) describes its
stdio-to-remote role. Its existence is not evidence that the reported Grogbot
uses that exact package or version. Grok web, Grok Bot, Grogbot, and Hermes must
not be treated as one client with one lifecycle.

The proposed user experience is to connect once, then run one client-side sync
action when a release requires it. That action checks the configured target,
reads authenticated host metadata, updates managed instruction files, and
refreshes the owned connection where the client provides a supported mechanism.
For provider-managed clients, it renders the exact manual action instead.
It never installs a Things Cloud host on a client-only machine.

Verification must run through the connection the agent will actually use.
A successful temporary HTTP probe does not verify a stale stdio bridge. The
check should compare actual discovery, then make a bounded read of a known test
item. Routine acceptance additionally needs a receiver run. Required instruction
installation should be verified by file checksums when available. A prompt the
provider does not expose for read-back remains unverified.

If a bridge watchdog later proves necessary, give it bounded retries and drain
in-flight calls before reconnecting where possible. Never replay uncertain
mutations with a new request ID. A schema validation failure after a write does
not establish that the write failed. Use the existing identical-request retry
and receipt recovery contract. Report authentication and reachability failures
separately from schema drift. Preserve public health's liveness-only response.

Implement the work in this order:

1. Fix and document output evolution, with a previous-release compatibility
   fixture. Test nested additive properties, existing known-field constraints,
   and strict rejection of unsupported inputs. Document the one-time refresh.
2. Generate the complete client bundle and impact manifest from existing assets.
   Bind discovery fingerprints to actual advertised tools. Verify archive
   completeness, deterministic checksums, and host-release selection on rollback.
3. Extend client diagnostics and connection guidance. Keep host deployment checks
   distinct from client compatibility checks. Exercise direct HTTP, a necessary
   stdio bridge, and a manual hosted connector. Prove an intentionally stale
   client is detected or clearly reported as unobservable.
4. Add bridge lifecycle automation only if those checks establish a remaining
   problem that the client cannot already handle.

Audit verification: 138 existing tests passed across deployment, doctor,
client configuration, server, and v2 contract tests. The separate synthetic
previous-release probe reproduced the output rejection described above.
No live client, remote deployment, instruction installation, or Things mutation
was performed. Runtime code is unchanged.
