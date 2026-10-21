"""EPSM adapter for the prepared evaluation executor.

Importing the generic Workflows package does not import the facade or engines.
Only execution of a prepared EPSM case requires the corresponding packages.
"""

from pathlib import Path


def _simulation_options(simulation):
    """Restore typed engine settings from a frozen JSON case without mutation."""
    options = dict(simulation)
    if (
        options.get("engine", "energyplus") == "energyplus"
        and options.get("settings") is not None
    ):
        from epsm_core import CompilerSettings

        options["settings"] = CompilerSettings.model_validate(options["settings"])
    return options


def evaluate(payload):
    """Reconstruct frozen inputs and use the same Building.simulate API locally/remotely."""
    from epsm import Building, Weather, AssessmentSettings, EvaluationCache

    building = Building.from_dict(payload["building"])
    options = _simulation_options(payload.get("simulation", {}))
    weather = options.pop("weather")
    if isinstance(weather, dict):
        weather = Weather.from_dict(weather)
    assessment = options.pop("assessment", None)
    if assessment is not None:
        building.assessment = AssessmentSettings.from_dict(assessment)
    execution = payload.get("_execution", {})
    root = Path(execution["directory"]) if execution else None
    cache_directory = payload.get("cache_directory", root / "cache" if root else None)
    if cache_directory is not None:
        options["cache"] = EvaluationCache(Path(cache_directory))
    if root is not None:
        options.setdefault(
            "directory",
            root / "artifacts" / execution["case_id"] / execution["attempt_id"],
        )
    result = building.simulate(weather=weather, **options)
    if payload.get("result_storage") == "bundle":
        if root is None:
            raise ValueError("Bundle results require prepared execution context")
        from hashlib import sha256
        from epsm import validate_outputs

        bundle = root / "artifacts" / execution["case_id"] / execution["attempt_id"] / "bundle"
        result.save_bundle(bundle)
        return {"format": "epsm-evaluation-reference/1",
                "path": bundle.relative_to(root).as_posix(),
                "manifest_sha256": sha256((bundle / "manifest.json").read_bytes()).hexdigest(),
                "evaluation_id": result.id,
                "coverage": validate_outputs(result, payload.get("output_requirements"))}
    return result
