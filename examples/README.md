# Examples

1. [Study authoring](01_study_authoring.ipynb) starts with a Monte Carlo template,
   saves its specification and inspects unresolved requirements. It needs the
   Workflows dependencies and a notebook kernel; it uses no network or engine.
2. [Reusable designs](reusable_design.py) shows the existing callback design API.

The complete installed six-evaluation integration gate belongs to EPSM's
`scripts/check_study_platform.py`. It requires EnergyPlus 24.2, RC, LCA and the
coordinated Studies/Workflows packages. Its illustrative assumptions test software
handoffs, not agreement with measured buildings.
