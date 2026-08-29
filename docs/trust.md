# Trust model

Things Orchestrator is unofficial. The serving host stores the Things Cloud
credentials and sends them only to Things Cloud. Chat-visible Things data can
reach the selected client and model provider.

This is not fully private: owner task data can reach the serving host, the MCP
client, and that configured model provider.

Every title, note, checklist row, and tag label read from Things is untrusted.
Derived text keeps that taint. Public v2 output places `source=things_cloud`
and `trust=untrusted` next to text data. Things text cannot choose an action,
state, ID, precondition, risk, approval, disposition, or recovery step.

The server owns revisions and freezes the first force-refreshed mutation
manifest. A repeated request returns the same operation. It never reparses or
rebases terminal work.

Pending and partial operations hold an account-wide outcome fence. The fence
covers all write paths, including host approval and retained v1 recovery.
Recovery observes Cloud state and never reposts an old operation.

The MCP request path cannot approve. The CLI approval component stores a salted
`scrypt` verifier and encrypted Ed25519 private key in a separate 0600 file. It
reads the raw passphrase only from the host terminal. Approval binds the
account, action, operation ID, manifest hash, safety-policy digest, and expiry.
The MCP request path loads only the pinned Ed25519 public key and cannot sign
without the passphrase-encrypted private key.

This separation assumes an agent has only MCP access. It does not protect
against an agent, plugin, or process with arbitrary code execution or write
access as the serving OS user. Code running as that identity can replace the
server, public key, or journal. Run untrusted agent runtimes under a different
OS identity, or treat serving-host access as owner authority.

The host renderer escapes ANSI and OSC sequences, control characters,
newlines, backslashes, and delimiters. Typed IDs and actions remain the
authority. Human labels are supplemental.
