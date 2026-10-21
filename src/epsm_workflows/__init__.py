"""Reusable EPSM study designs, renovation search and prepared execution."""

from ._version import __version__ as __version__
from .design import Parameter, grid, latin_hypercube, sensitivity_design, analyse_sensitivity, parametric_cases
from .renovation import RenovationNode, RenovationTree, optimise_renovations
from .vera import VeraExecutor
from .workflow import WorkflowSpec, ParameterSpec, OutputRequirement, template

__all__ = ["Parameter", "grid", "latin_hypercube", "sensitivity_design", "analyse_sensitivity",
           "WorkflowSpec", "ParameterSpec", "OutputRequirement", "template", "read_attempts", "validate_plan", "inspect_plan", "cancel", "resume",
           "parametric_cases", "PreparedEvaluation", "prepare", "run_local", "read_results", "RenovationNode",
           "RenovationTree", "optimise_renovations", "VeraExecutor"]


def __getattr__(name):
    # Keep python -m epsm_workflows.execution free of duplicate module loading.
    if name in {"PreparedEvaluation", "prepare", "run_local", "read_results", "read_attempts", "validate_plan", "inspect_plan", "cancel", "resume"}:
        from . import execution
        value = getattr(execution, name)
        globals()[name] = value
        return value
    raise AttributeError(name)
