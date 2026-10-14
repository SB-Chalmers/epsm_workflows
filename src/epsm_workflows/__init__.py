"""Reusable EPSM study designs, renovation search and prepared execution."""

from ._version import __version__ as __version__
from .design import Parameter, grid, latin_hypercube, sensitivity_design, analyse_sensitivity, parametric_cases
from .renovation import RenovationNode, RenovationTree, optimise_renovations
from .vera import VeraExecutor

__all__ = ["Parameter", "grid", "latin_hypercube", "sensitivity_design", "analyse_sensitivity",
           "parametric_cases", "PreparedEvaluation", "prepare", "run_local", "read_results", "RenovationNode",
           "RenovationTree", "optimise_renovations", "VeraExecutor"]


def __getattr__(name):
    # Keep python -m epsm_workflows.execution free of duplicate module loading.
    if name in {"PreparedEvaluation", "prepare", "run_local", "read_results"}:
        from . import execution
        value = getattr(execution, name)
        globals()[name] = value
        return value
    raise AttributeError(name)
