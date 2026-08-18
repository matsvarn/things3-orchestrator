# Review completeness stays behind the three tools

A production migration showed the three tools were still too shallow:
callers had to chain many views to see the library, desired-state
equality ignored Someday, and the published schema accepted inputs the
workspace then rejected. We deepened `things_read` with `area`, `audit`,
`diagnostics`, and `ids`, and we made desired-state and batch projection
complete. We rejected extra tools and JSON Schema unions because the
existing three-tool, flat-schema contract is the public seam.
