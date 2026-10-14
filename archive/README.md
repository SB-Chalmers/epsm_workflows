# Archived workflow studies

On 14 October 2026, the maintainer retired the existing named studies, campaign
implementations and their compatibility imports. The reusable study harness remains
active in `src/epsm_workflows`: preparation, local/Vera execution and resume,
parametric and sensitivity designs, generic renovation search, and shared helpers.

The [2026-10-14 snapshot](2026-10-14/) preserves 226 files from
`epsm_workflows` revision `3ad48a8edda47cd418ae53c2f3feffc87a524b04` at their original relative paths. This
includes study-specific source, tests, examples, fixed campaign configurations,
documentation, material assumptions, and the original top-level README, package
configuration and database adapter document. The [manifest](2026-10-14/manifest.json) records every preserved
file's original path, size and SHA-256 checksum.

These files are retained for later inspection. They are excluded from package
installation and default test collection. The archival decision makes no promise
that old imports, commands, environments or results remain reproducible. References
within the snapshot retain their historical wording and may point to moved or
unavailable resources. No archived implementation has been repaired or revalidated.

The national database design starter remains outside this archive; it is separate
from the retired executable studies.
