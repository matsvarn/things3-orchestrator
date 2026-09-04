# Trust model

Things Orchestrator is unofficial. The serving host stores the Things Cloud
credentials and sends them only to Things Cloud. Chat-visible Things data can
reach the selected client and model provider.

This is not fully private: owner task data can reach the serving host, the MCP
client, and that configured model provider.

Routines send data to one more system. When enabled, the host polls the
unsupported Things Cloud history feed and sends a metadata-only event to the
configured receiver. That event contains a public task ID but no title, notes,
checklist, project content, account email, or private history key. The receiver
can learn task content only through its separately authorized MCP connection.
The webhook secret and MCP bearer are different credentials.

Delivery is at-least-once. The receiver must deduplicate the opaque event ID.
The generated service marker records that the owner started the supervised
path; it is not a security attestation against a local process owner. Unknown
history versions and malformed relevant fields stop cursor advancement instead
of guessing.

Every title, note, checklist row, and tag label read from Things is untrusted.
Derived text keeps that taint. Public v2 output places `source=things_cloud`
and `trust=untrusted` next to text data. Things text cannot choose an action,
state, ID, precondition, risk, approval, disposition, or recovery step.

The server owns revisions and freezes the first force-refreshed mutation
manifest. A repeated request returns the same operation. It never reparses or
rebases terminal work.

Only an unresolved `pending` operation holds the account-wide outcome fence.
An exact retry observes Cloud state and never reposts the frozen operation.
Fully classified `partial` outcomes are terminal and carry an immutable
read-back receipt.

The shared MCP bearer, or access to the stdio server, authorizes every bounded
v2 mutation. There is no per-client identity or human-presence claim. Keep the
bearer private and give it only to agents that may write to the account.

An agent, plugin, or process with arbitrary code execution or write access as
the serving OS user can replace the server or journal. Run untrusted agent
runtimes under a different OS identity, or treat serving-host access as full
write authority.

The host renderer escapes ANSI and OSC sequences, control characters,
newlines, backslashes, and delimiters. Typed IDs and actions remain the
authority. Human labels are supplemental.
