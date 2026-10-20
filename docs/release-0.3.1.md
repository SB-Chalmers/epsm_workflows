# Workflows 0.3.1 — license and distribution metadata

This patch includes the root MIT license in wheels and source archives and
declares `License-Expression: MIT` in Python distribution metadata. The holder
notice follows the existing `Copyright (c) 2026 EPSM Development contributors`
source notices in the prepared-settings and runtime tests. Existing source
notices are retained. The package metadata also supplies its description, README
and repository link. The build requires Setuptools 77 or later for these license
fields.

The 0.3.0 tag remains unchanged. Version 0.3.1 supersedes its incomplete packaging;
there is no scientific calculation, public signature or dependency-bound change.
`_version.py` remains the authoritative version for both the public import and
dynamic distribution metadata.

Prepared plans retain schema 3 and its engine settings, per-case runtime bindings
and batch verification. There is no prepared-plan migration. Runtime identities
include package versions, so prepare a new plan when changing the installed
package combination. Historical plans and results remain retained evidence.

Workflows continues to delegate physical calculations to their owning packages.
The common EPSM purchased-energy boundary covers space heating and ventilation
fans; this packaging patch does not extend that boundary. The complete updated
EPSM cohort has its own required checks after adopting this patch.
