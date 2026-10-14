"""Explicit compatibility boundary for historical, paper-specific workflows."""
from importlib import import_module


def load(module):
    try:
        return import_module(module)
    except ModuleNotFoundError as error:
        if error.name in {"epsm_studies", "epsm_studies.legacy_workflows"}:
            raise ModuleNotFoundError(
                "This historical study moved to epsm_studies. Install epsm_studies "
                "alongside epsm_workflows, or use the reusable design, renovation "
                "and execution APIs for new work."
            ) from error
        raise
