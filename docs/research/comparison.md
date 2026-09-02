# Credential boundary comparison

Reviewed on 2026-08-14. Recheck the linked sources before you publish this
comparison again. Read this when choosing between the local Mac MCP, this
project, and a hosted Cloud login.

The key question is whether a Things Cloud password is used. If it is used,
the next question is who runs the host that stores it.

| Project | Things access | Host | Cloud password |
| --- | --- | --- | --- |
| [`hald/things-mcp`](https://github.com/hald/things-mcp) | Local Mac database and Things URL scheme | Your Mac | Not used by the MCP |
| Things Orchestrator | Unofficial Things Cloud protocol | Your chosen host | Plaintext file with mode 0600; sent to Things Cloud on requests |
| [`thingscloudmcp.com`](https://thingscloudmcp.com) | Unofficial Things Cloud protocol | Service operator's host | Submitted to and stored by the hosted service |

The local project's README requires macOS and Things 3. It states that the
server uses
[`Things.py`](https://github.com/hald/things-mcp#things-mcp-server) and the
[Things URL scheme](https://github.com/hald/things-mcp#things-mcp-server).

The hosted project's README describes a public endpoint. It accepts Things
Cloud credentials through OAuth or Basic authentication. See the reviewed
[README snapshot](https://github.com/wbopan/things-cloud-mcp/blob/32a4b90c091e8a1688d0c4c22c408bb1603ebd86/README.md#L3-L28).
Its OAuth database schema stores the password in `TEXT` columns. See the
reviewed
[source snapshot](https://github.com/wbopan/things-cloud-mcp/blob/32a4b90c091e8a1688d0c4c22c408bb1603ebd86/oauth.go#L67-L130).

Things Orchestrator stores one account on the owner's host. See the
[trust boundary](../trust.md) and the local credential implementation in
[`cloud.py`](../../src/things_orchestrator/cloud.py).

These differences do not make any option risk-free. Both Cloud options use an
unofficial protocol. Cultured Code can change that protocol or disable an
account.
