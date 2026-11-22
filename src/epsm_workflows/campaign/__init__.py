# SPDX-License-Identifier: MIT
"""Vera training-data campaigns: frozen studies, portable plans, sharded results.

Author and pack a study locally; Vera only needs ``epsm_core``, ``duckdb`` and
``numpy`` to execute the packed plan and ``finalize`` the handoff database.
"""

from .study import StudySpec, load_study

__all__ = ["StudySpec", "load_study"]
