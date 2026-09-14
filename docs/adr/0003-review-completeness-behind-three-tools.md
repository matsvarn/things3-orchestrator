# Review completeness stays behind the three tools

## Status

Superseded by ADR 0006 and ADR 0007. The three-tool design below remains
only as a historical record and is not the current implementation contract.

A production migration showed the three tools were still too shallow:
callers had to chain many views to see the library, desired-state
equality ignored Someday, and the published schema accepted inputs the
workspace then rejected. We deepened `things_read` with `area`, `audit`,
`diagnostics`, and `ids`, and we made desired-state and batch projection
complete. We rejected extra tools and JSON Schema unions because the
existing three-tool, flat-schema contract is the public seam.
