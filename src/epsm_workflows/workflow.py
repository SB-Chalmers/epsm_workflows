# SPDX-License-Identifier: MIT
"""Versioned study authoring over existing physical and prepared-execution APIs.

Templates, exported JSON Schema and persisted records use the same typed models.
Authoring does not execute engines. Explicit resolution, preview, and preparation
have separate responsibilities; preparation freezes independent cases and assets.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import random
from copy import deepcopy
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ._events import event
from .distributions import Marginal, distribution_from_dict, sample_quantile, uniform
from .records import atomic_json, digest, json_value

logger = logging.getLogger(__name__)


class _Definition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Sampling(_Definition):
    method: Literal["lhs", "monte_carlo", "grid", "explicit"] = "lhs"
    samples: int = Field(default=100, ge=1)
    seed: int = 0
    correlation: dict | None = None


class SyntheticSource(_Definition):
    kind: Literal["synthetic"]
    shape: Literal["rectangle", "l_shape", "u_shape"] = "rectangle"
    dimensions: dict = Field(default_factory=dict)
    geometry: dict = Field(default_factory=dict)


class DrawnSource(_Definition):
    kind: Literal["drawn"]
    footprint: dict
    geometry: dict = Field(default_factory=dict)


class RecordSource(_Definition):
    kind: Literal["record"]
    record: dict
    geometry: dict = Field(default_factory=dict)


SubjectSource = Annotated[
    SyntheticSource | DrawnSource | RecordSource, Field(discriminator="kind")
]


class SuppliedWeatherRequest(_Definition):
    kind: Literal["epw", "tmy"]
    path: str
    calendar_year: int | None = None
    provider: str | None = None
    source_period: list[int] | None = None


class AMYBatchRequest(_Definition):
    kind: Literal["amy_batch"]
    provider: Literal["smhi"] = "smhi"
    years: list[int] = Field(min_length=1)
    locations: list[dict] = Field(min_length=1)
    subjects_by_location: dict[str, list[str]] = Field(default_factory=dict)
    compiler_options: dict | None = None
    morphing: list[dict] = Field(default_factory=list)
    options: dict = Field(default_factory=dict)


WeatherRequest = Annotated[
    SuppliedWeatherRequest | AMYBatchRequest, Field(discriminator="kind")
]


class ParameterSpec(_Definition):
    """One registered marginal with explicit scope, applicability and reference."""

    binding: str
    kind: Literal["continuous", "integer", "categorical"] = "continuous"
    unit: str = ""
    min: float | None = None
    max: float | None = None
    choices: list[Any] | None = None
    distribution: Marginal | None = None
    bounds_policy: Literal["truncate", "validate"] = "truncate"
    baseline: Any = "inherit"
    scope: Literal["shared", "per_building", "per_group"] = "shared"
    selector: dict[str, Any] = Field(default_factory=dict)
    applies_when: dict[str, Any] = Field(default_factory=dict)

    def __repr__(self):
        from ._inspection import inspection

        return inspection(
            type(self).__name__,
            binding=self.binding,
            kind=self.kind,
            choices=len(self.choices) if self.choices is not None else 0,
            scope=self.scope,
            unit=self.unit,
        )

    __str__ = __repr__

    def summary(self):
        """Inspect detached loaded domain, assumptions and selector metadata."""
        return self.model_dump(mode="json")

    @model_validator(mode="after")
    def _domain(self):
        from .bindings import native_unit, validate_selector

        if not self.binding.strip():
            raise ValueError("binding must be nonempty")
        validate_selector(self.selector, binding=self.binding)
        expected_unit = native_unit(self.binding)
        legacy_native = self.unit == "native" and self.binding in {
            "envelope.element_u_value", "geometry.footprint_area_m2",
        }
        if self.unit and expected_unit != "native" and self.unit != expected_unit and not legacy_native:
            raise ValueError(
                f"{self.binding} uses {expected_unit}; no unit conversion is performed"
            )
        if not self.unit:
            object.__setattr__(self, "unit", expected_unit)
        if self.choices is not None and not self.choices:
            raise ValueError("choices must be nonempty")
        if self.kind == "integer" and self.choices is not None:
            for value in self.choices:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or int(value) != value
                    or (self.min is not None and value < self.min)
                    or (self.max is not None and value > self.max)
                ):
                    raise ValueError(
                        "integer choices must be finite integral numbers inside declared bounds"
                    )
        json.dumps(json_value(self.choices), allow_nan=False)
        json.dumps(json_value(self.baseline), allow_nan=False)
        if self.kind == "categorical":
            if not self.choices or len(
                {json.dumps(x, sort_keys=True) for x in self.choices}
            ) != len(self.choices):
                raise ValueError("categorical parameters need unique choices")
        elif self.choices is None and (
            self.min is None or self.max is None or self.min > self.max
        ):
            raise ValueError("numeric parameters need ordered min/max bounds")
        if (
            self.kind == "integer"
            and self.choices is None
            and (int(self.min) != self.min or int(self.max) != self.max)
        ):
            raise ValueError("integer parameters need integer bounds")
        if (
            self.kind == "integer"
            and self.distribution is not None
            and self.distribution.kind not in {"uniform", "discrete", "categorical"}
        ):
            raise ValueError(
                "Integer parameters require explicit discrete probabilities or a uniform integer domain; continuous distributions are not rounded"
            )
        if self.distribution is not None and self.distribution.kind == "log_uniform" and (
            self.choices is not None or self.min is None or self.max is None or not 0 < self.min < self.max
        ):
            raise ValueError("Log-uniform parameters require increasing positive bounds")
        return self


class Subject(_Definition):
    id: str = Field(min_length=1)
    building: dict | None = None
    group: str | None = None
    source: SubjectSource | None = None
    location: dict = Field(default_factory=dict)


class WeatherCase(_Definition):
    id: str = Field(min_length=1)
    weather: dict | None = None
    subjects: list[str] | None = None
    request: WeatherRequest | None = None
    metadata: dict = Field(default_factory=dict)


class Scenario(_Definition):
    id: str = Field(min_length=1)
    subject: str
    building: dict

    @model_validator(mode="after")
    def _identity(self):
        if self.id == "baseline" or self.id.startswith("sample-"):
            raise ValueError(
                "baseline and sample- identifiers are reserved for generated cases"
            )
        return self


class OutputRequirement(_Definition):
    name: str
    resolution: Literal["hourly", "monthly", "annual", "assessment"] = "annual"
    required: bool = True
    level: Literal["building", "zone"] = "building"
    unit: str | None = None

    def __repr__(self):
        from ._inspection import inspection

        return inspection(
            type(self).__name__,
            name=self.name,
            resolution=self.resolution,
            required=self.required,
            level=self.level,
            unit=self.unit,
        )

    __str__ = __repr__

    def summary(self):
        """Inspect the declared output boundary using detached loaded metadata."""
        return self.model_dump(mode="json")


class ExecutionSettings(_Definition):
    executor: Literal["local", "vera"] = "local"
    workers: int = Field(default=1, ge=1)


class WorkflowDefinition(_Definition):
    """The single typed source for workflow templates, JSON and form schema."""

    format: Literal["epsm-workflow/1"] = "epsm-workflow/1"
    template: Literal["lhs", "monte_carlo", "comparative"] = "lhs"
    template_version: Literal[1] = 1
    sampling: Sampling = Field(default_factory=Sampling)
    subjects: list[Subject] = Field(default_factory=list)
    model_inputs: dict[str, Any] = Field(default_factory=dict)
    parameters: list[ParameterSpec] = Field(default_factory=list)
    scenarios: list[Scenario] = Field(default_factory=list)
    weather: list[WeatherCase] = Field(default_factory=list)
    engines: list[Literal["energyplus", "epsm_rc"]] = Field(
        default_factory=lambda: ["epsm_rc"]
    )
    engine_settings: dict[Literal["energyplus", "epsm_rc"], dict] = Field(
        default_factory=dict
    )
    baseline: bool = False
    outputs: list[OutputRequirement] = Field(
        default_factory=lambda: [
            OutputRequirement(
                name="space_heating_demand_kwh", resolution="hourly", unit="kWh"
            ),
            OutputRequirement(name="delivered_energy", resolution="annual"),
        ]
    )
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)

    @model_validator(mode="after")
    def _identities(self):
        from .bindings import canonical_binding, validate_selector

        for name, rows in (("subject", self.subjects), ("weather", self.weather)):
            if len({row.id for row in rows}) != len(rows):
                raise ValueError(f"Duplicate {name} identity")
        if not self.engines or len(set(self.engines)) != len(self.engines):
            raise ValueError("Select unique engine identities")
        if len({canonical_binding(p.binding) for p in self.parameters}) != len(
            self.parameters
        ):
            raise ValueError("Duplicate parameter binding")
        inputs = set()
        for binding, record in self.model_inputs.items():
            canonical = canonical_binding(binding)
            if canonical in inputs:
                raise ValueError("Duplicate physical binding aliases")
            inputs.add(canonical)
            if (
                not isinstance(record, dict)
                or "value" not in record
                or set(record) - {"value", "selector"}
            ):
                raise ValueError(
                    "Fixed inputs require value and optional selector fields"
                )
            validate_selector(record.get("selector", {}), binding=binding)
        if len({(s.subject, s.id) for s in self.scenarios}) != len(self.scenarios):
            raise ValueError("Duplicate scenario identity for a subject")
        return self


class WorkflowSpec:
    """Editable workflow draft; each operation stores a newly validated definition.

    Use ``template`` to start an incomplete study, configure concrete subjects and
    weather, then ``preview`` and ``prepare``. No simulation starts implicitly.
    ``to_dict`` returns a detached copy. A prepared directory is immutable.
    """

    def __init__(self, definition=None):
        json.dumps(json_value(definition or {}), allow_nan=False)
        self._definition = WorkflowDefinition.model_validate(definition or {})

    @classmethod
    def template(cls, method="lhs", *, samples=100, seed=0):
        """Load a validated template without selecting physical assumptions."""
        if method not in {"lhs", "monte_carlo", "comparative"}:
            raise ValueError("Unknown parametric workflow template")
        return cls(
            {
                "template": method,
                "baseline": method == "comparative",
                "sampling": {
                    "method": "grid" if method == "comparative" else method,
                    "samples": samples,
                    "seed": seed,
                },
            }
        )

    @classmethod
    def from_dict(cls, value):
        return cls(deepcopy(value))

    def to_dict(self):
        return self._definition.model_dump(mode="json")

    @staticmethod
    def schema():
        """Return generated JSON Schema; capability schemas describe owner fields."""
        from .bindings import capabilities

        schema = WorkflowDefinition.model_json_schema()
        registry = capabilities()
        schema["$defs"]["ParameterSpec"]["properties"]["binding"]["enum"] = sorted(
            registry
        )
        schema["properties"]["model_inputs"]["propertyNames"] = {
            "enum": sorted(registry)
        }
        schema["x-epsm-inputs"] = registry
        # Acquisition containers belong to Workflows; their payload schemas come
        # directly from physical/provider owners, without copying their validators.
        from epsm_core import Footprint

        _embed_schema(
            schema,
            "DrawnSource",
            "footprint",
            Footprint.model_json_schema(),
            "CoreFootprint",
        )
        try:
            from pydantic import TypeAdapter
            from smhi2epw import MorphConfig, WeatherLocation
        except ImportError:
            schema["x-epsm-weather-acquisition"] = {
                "status": "unavailable",
                "install": "epsm_workflows[weather]",
            }
        else:
            _embed_schema(
                schema,
                "AMYBatchRequest",
                "locations",
                TypeAdapter(list[WeatherLocation]).json_schema(),
                "WeatherLocation",
            )
            _embed_schema(
                schema,
                "AMYBatchRequest",
                "morphing",
                TypeAdapter(list[MorphConfig]).json_schema(),
                "WeatherMorph",
            )
            schema["x-epsm-weather-acquisition"] = {
                "status": "supported",
                "owner": "smhi2epw",
            }
        schema["x-epsm-correlation"] = {
            "status": "unsupported",
            "reason": "Only independent marginal sampling is available",
        }
        return schema

    @staticmethod
    def capabilities():
        from .bindings import capabilities

        return capabilities()

    @property
    def id(self):
        return digest(self.to_dict())

    def __repr__(self):
        d = self._definition
        return f"WorkflowSpec(template={d.template!r}, subjects={len(d.subjects)}, parameters={len(d.parameters)})"

    def _replace(self, **changes):
        value = {**self.to_dict(), **changes}
        json.dumps(json_value(value), allow_nan=False)
        self._definition = WorkflowDefinition.model_validate(value)
        return self

    def set(self, binding, value, *, selector=None):
        """Set workflow configuration or one registered physical input explicitly."""
        data = self.to_dict()
        aliases = {"rc": "epsm_rc", "eplus": "energyplus"}
        if binding == "engines":
            return self._replace(engines=[aliases.get(x, x) for x in value])
        if binding == "engine_settings":
            if not isinstance(value, dict):
                raise ValueError("engine_settings requires an engine-keyed mapping")
            settings = {
                aliases.get(engine, engine): json_value(options)
                for engine, options in value.items()
            }
            if len(settings) != len(value):
                raise ValueError("Duplicate engine aliases in engine_settings")
            return self._replace(engine_settings=settings)
        if binding in {
            "outputs",
            "baseline",
            "scenarios",
            "weather",
            "subjects",
        }:
            return self._replace(**{binding: json_value(value)})
        if binding in {
            "sampling.method",
            "sampling.samples",
            "sampling.seed",
            "sampling.correlation",
            "execution.workers",
            "execution.executor",
        }:
            group, key = binding.split(".")
            data[group][key] = value
            return self._replace(**{group: data[group]})
        if binding not in self.capabilities():
            raise ValueError(f"Unknown physical binding: {binding}")
        from .bindings import canonical_binding, serialize_binding

        identity = canonical_binding(binding)
        data["model_inputs"] = {
            key: record
            for key, record in data["model_inputs"].items()
            if canonical_binding(key) != identity
        }
        data["model_inputs"][binding] = {
            "value": serialize_binding(binding, value),
            "selector": selector or {},
        }
        data["parameters"] = [
            p for p in data["parameters"] if canonical_binding(p["binding"]) != identity
        ]
        return self._replace(
            model_inputs=data["model_inputs"], parameters=data["parameters"]
        )

    def vary(
        self,
        binding,
        *,
        min=None,
        max=None,
        choices=None,
        distribution=None,
        bounds_policy="truncate",
        baseline="inherit",
        scope="shared",
        selector=None,
        kind=None,
        unit=None,
        applies_when=None,
    ):
        """Declare one marginal, retaining its domain, reference and sampling scope."""
        from .bindings import serialize_binding

        if binding not in self.capabilities():
            raise ValueError(f"Unknown physical binding: {binding}")
        if distribution is None:
            if self._definition.sampling.method == "monte_carlo":
                raise ValueError("Monte Carlo requires an explicit distribution")
            distribution = uniform()
        record = ParameterSpec(
            binding=binding,
            min=min,
            max=max,
            choices=[serialize_binding(binding, choice) for choice in choices]
            if choices is not None
            else None,
            kind=kind
            or (
                "categorical"
                if choices is not None
                else "integer"
                if binding == "geometry.storeys"
                else "continuous"
            ),
            distribution=distribution_from_dict(distribution),
            bounds_policy=bounds_policy,
            baseline=baseline
            if baseline == "inherit"
            else serialize_binding(binding, baseline),
            scope=scope,
            selector=selector or {},
            unit=unit or "",
            applies_when=applies_when or {},
        )
        from .bindings import canonical_binding

        identity = canonical_binding(binding)
        parameters = [
            p
            for p in self.to_dict()["parameters"]
            if canonical_binding(p["binding"]) != identity
        ]
        parameters.append(record.model_dump(mode="json"))
        inputs = self.to_dict()["model_inputs"]
        inputs = {
            key: record
            for key, record in inputs.items()
            if canonical_binding(key) != identity
        }
        return self._replace(
            parameters=parameters,
            model_inputs=inputs,
            baseline=self._definition.baseline or baseline != "inherit",
        )

    def add_subject(
        self, building=None, *, id=None, group=None, source=None, location=None
    ):
        """Capture a portable model or an explicit unresolved source request."""
        payload = json_value(building) if building is not None else None
        name = id or getattr(building, "id", None)
        if not name:
            raise ValueError("Supply a stable subject id")
        rows = self.to_dict()["subjects"]
        rows.append(
            Subject(
                id=name,
                building=payload,
                group=group,
                source=json_value(source),
                location=location or {},
            ).model_dump(mode="json")
        )
        return self._replace(subjects=rows)

    def add_weather(self, weather=None, *, id=None, subjects=None, request=None):
        """Capture inspected weather or an unresolved public provider request."""
        if isinstance(weather, (str, Path)):
            from epsm_core import Weather

            weather = Weather.from_epw(weather)
        payload = json_value(weather) if weather is not None else None
        name = id or (payload or {}).get("weather_sha256")
        if not name:
            raise ValueError("Supply a stable weather id")
        rows = self.to_dict()["weather"]
        rows.append(
            WeatherCase(
                id=name, weather=payload, subjects=subjects, request=json_value(request)
            ).model_dump(mode="json")
        )
        return self._replace(weather=rows)

    def add_scenario(self, name, building, *, subject=None):
        """Retain a material-backed alternative made through public renovation APIs."""
        if subject is None:
            if len(self._definition.subjects) != 1:
                raise ValueError("Select a subject for a portfolio scenario")
            subject = self._definition.subjects[0].id
        rows = self.to_dict()["scenarios"]
        rows.append(
            Scenario(
                id=name, subject=subject, building=json_value(building)
            ).model_dump(mode="json")
        )
        return self._replace(scenarios=rows, baseline=True)

    def _sampling_seed(self, identity):
        key = hashlib.sha256(
            f"{self._definition.sampling.seed}:{identity}".encode()
        ).digest()
        return int.from_bytes(key, "big")

    def _rng(self, identity):
        return random.Random(self._sampling_seed(identity))

    def samples(self, *, scope="shared"):
        """Generate deterministic independent marginals without physical mutation."""
        from .design import Parameter, grid, latin_hypercube

        d = self._definition
        parameters = d.parameters
        if not parameters:
            return [{}]
        if d.sampling.method in {"grid", "explicit"}:
            for p in parameters:
                if not p.choices:
                    raise ValueError("Grid/explicit parameters require finite choices")
            return grid({p.binding: p.choices for p in parameters})
        columns = []
        for p in parameters:
            identity = p.binding + ":" + (scope if p.scope != "shared" else "shared")
            if d.sampling.method == "lhs":
                probabilities = [
                    row[p.binding]
                    for row in latin_hypercube(
                        [Parameter(p.binding, 0, 1)],
                        d.sampling.samples,
                        seed=self._sampling_seed(identity),
                    )
                ]
            else:
                rng = self._rng(identity)
                probabilities = [rng.random() for _ in range(d.sampling.samples)]
            choices = p.choices
            if p.kind == "integer" and choices is None:
                choices = list(range(int(p.min), int(p.max) + 1))
            columns.append(
                [
                    sample_quantile(
                        p.distribution or uniform(),
                        max(q, 1e-15),
                        lower=p.min,
                        upper=p.max,
                        choices=choices,
                        bounds_policy=p.bounds_policy,
                    )
                    for q in probabilities
                ]
            )
        return [
            dict(zip((p.binding for p in parameters), row)) for row in zip(*columns)
        ]

    def validate(self):
        """Return authoring requirements without acquisition, engine runs or writes."""
        diagnostics = []

        def issue(code, field, message):
            diagnostics.append(
                {"code": code, "field": field, "message": message, "severity": "error"}
            )

        d = self._definition
        from .bindings import canonical_binding

        if d.sampling.correlation is not None:
            issue(
                "unsupported_correlation",
                "sampling.correlation",
                "Only independent marginal sampling is available",
            )
        if d.template == "comparative" and not d.parameters and not d.scenarios:
            issue(
                "missing_candidates",
                "scenarios",
                "Declare candidate alternatives or a parameter design",
            )
        if not d.subjects:
            issue("missing_subjects", "subjects", "Select at least one building")
        for subject in d.subjects:
            if subject.building is None:
                issue(
                    "unresolved_subject",
                    f"subjects.{subject.id}",
                    "Resolve the declared source explicitly",
                )
        if not d.weather:
            issue("missing_weather", "weather", "Select at least one weather artifact")
        subject_ids = {x.id for x in d.subjects}
        group_ids = {x.group for x in d.subjects if x.group is not None}
        selections = [
            (binding, value.get("selector", {}))
            for binding, value in d.model_inputs.items()
        ]
        selections.extend(
            (parameter.binding, parameter.selector) for parameter in d.parameters
        )
        for binding, selector in selections:
            if not set(selector.get("subjects", [])) <= subject_ids:
                issue(
                    "unknown_subject",
                    binding,
                    "Input selector identifies an unknown subject",
                )
            if not set(selector.get("groups", [])) <= group_ids:
                issue(
                    "unknown_group",
                    binding,
                    "Input selector identifies an unknown group",
                )
        for weather in d.weather:
            if weather.weather is None:
                issue(
                    "unresolved_weather",
                    f"weather.{weather.id}",
                    "Resolve the weather request explicitly",
                )
            if weather.subjects and not set(weather.subjects) <= subject_ids:
                issue(
                    "unknown_subject",
                    f"weather.{weather.id}",
                    "Weather assignment identifies an unknown building",
                )
        registry = self.capabilities()
        for binding in set(d.model_inputs) | {p.binding for p in d.parameters}:
            if binding not in registry:
                issue("unknown_binding", binding, "Unknown physical binding")
            elif registry[binding]["status"] != "supported":
                issue("unsupported_binding", binding, registry[binding]["reason"])
        for p in d.parameters:
            if p.scope == "per_group" and any(s.group is None for s in d.subjects):
                issue(
                    "missing_group",
                    p.binding,
                    "Per-group sampling requires an explicit group for every subject",
                )
            if d.sampling.method == "monte_carlo" and p.distribution is None:
                issue(
                    "missing_distribution",
                    p.binding,
                    "Monte Carlo requires explicit distributions",
                )
            for dependency in p.applies_when:
                if dependency not in registry:
                    issue(
                        "unknown_condition",
                        p.binding,
                        f"Unknown condition binding: {dependency}",
                    )
                elif registry[dependency]["status"] != "supported":
                    issue(
                        "unsupported_condition",
                        p.binding,
                        f"Unsupported condition binding: {dependency}",
                    )
            if any(
                canonical_binding(key) == canonical_binding(p.binding)
                for key in p.applies_when
            ):
                issue(
                    "cyclic_condition", p.binding, "A parameter cannot depend on itself"
                )
        try:
            _condition_order(d.parameters)
        except ValueError as error:
            issue("cyclic_condition", "parameters", str(error))
        for scenario in d.scenarios:
            if scenario.subject not in subject_ids:
                issue(
                    "unknown_subject",
                    f"scenarios.{scenario.id}",
                    "Scenario identifies an unknown subject",
                )
        return {"ready": not diagnostics, "diagnostics": diagnostics}

    def resolve_inputs(self, *, directory=None):
        """Resolve explicitly requested source/weather inputs through their owners.

        Synthetic/drawn geometry and portable DB records use EPSM/Core public
        assembly. AMY batch/morphing invokes installed smhi2epw operations only on
        this explicit call. Credentials and live clients are never serialized.
        """
        from epsm import Building
        from epsm_core import Footprint

        subjects = self.to_dict()["subjects"]
        for row in subjects:
            if row["building"] is not None:
                continue
            source = row.get("source") or {}
            kind = source.get("kind")
            if kind in {"synthetic", "drawn"}:
                if kind == "synthetic":
                    factory = {
                        "rectangle": Footprint.rectangle,
                        "l_shape": Footprint.l_shape,
                        "u_shape": Footprint.u_shape,
                    }.get(source.get("shape", "rectangle"))
                    if factory is None:
                        raise ValueError("Unsupported synthetic shape")
                    footprint = factory(**source.get("dimensions", {}))
                else:
                    footprint = Footprint.model_validate(source["footprint"])
                building = Building.from_footprint(
                    footprint, identifier=row["id"], **source.get("geometry", {})
                )
            elif kind == "record":
                from epsm_db import BuildingRecord

                building = Building.from_record(
                    BuildingRecord.from_dict(source["record"]),
                    **source.get("geometry", {}),
                )
            else:
                raise ValueError(f"Unsupported or unresolved subject source: {kind}")
            row["building"] = building.to_dict()
        weather = self.to_dict()["weather"]
        expanded = []
        for row in weather:
            if row["weather"] is not None:
                expanded.append(row)
                continue
            request = row.get("request") or {}
            if request.get("kind") in {"epw", "tmy"}:
                from epsm_core import Weather

                row["weather"] = Weather.from_epw(
                    request["path"], calendar_year=request.get("calendar_year")
                ).to_dict()
                expanded.append(row)
            elif request.get("kind") == "amy_batch":
                if directory is None:
                    raise ValueError(
                        "Weather acquisition needs an explicit output directory"
                    )
                expanded.extend(
                    _resolve_weather_batch(
                        row,
                        Path(directory) / ("weather-" + digest(row["id"])[:16]),
                        subject_ids={subject["id"] for subject in subjects},
                    )
                )
            else:
                raise ValueError(
                    "Unsupported weather request; supply a resolved weather artifact"
                )
        self._replace(subjects=subjects, weather=expanded)
        event(
            logger,
            "study.inputs.resolved",
            "Resolved declared study inputs",
            context={"workflow_id": self.id},
            details={"subjects": len(subjects), "weather": len(expanded)},
        )
        return self

    def _expanded(self, *, check_ready):
        from epsm import Building
        from epsm_core import Weather

        from .bindings import apply_bindings, canonical_binding, read_binding

        d = self._definition
        report = self.validate()
        if not report["ready"]:
            return [], report["diagnostics"]
        rows, diagnostics = [], []
        for subject in d.subjects:
            original = Building.from_dict(subject.building)
            fixed = {
                k: v["value"]
                for k, v in d.model_inputs.items()
                if _selected(v.get("selector", {}), subject)
            }
            selectors = {k: v.get("selector", {}) for k, v in d.model_inputs.items()}
            generates_context = "context.synthetic" in fixed or any(
                parameter.binding == "context.synthetic" and _selected(parameter.selector, subject)
                for parameter in d.parameters
            )
            deferred = {key: value for key, value in fixed.items()
                        if key in {"context.synthetic", "envelope.element_u_value"}
                        or (generates_context and (key == "context" or key.startswith("context.")))}
            try:
                base = apply_bindings(original, {key: value for key, value in fixed.items() if key not in deferred}, selectors=selectors)
                references = {
                    p.binding: p.baseline
                    for p in d.parameters
                    if p.baseline != "inherit" and _selected(p.selector, subject)
                }
                baseline = apply_bindings(
                    base,
                    {**(deferred if d.baseline else {}), **references},
                    selectors={**selectors, **{p.binding: p.selector for p in d.parameters}},
                )
            except (ValueError, TypeError) as error:
                diagnostics.append(
                    _diagnostic("invalid_inputs", subject.id, str(error))
                )
                continue
            alternatives = []
            if d.baseline:
                alternatives.append(("baseline", baseline, {}, True))
            scoped = {}
            try:
                for p in d.parameters:
                    identity = (
                        subject.id
                        if p.scope == "per_building"
                        else subject.group
                        if p.scope == "per_group"
                        else "shared"
                    )
                    if identity not in scoped:
                        scoped[identity] = self.samples(scope=identity)
                count = (
                    len(next(iter(scoped.values())))
                    if scoped
                    else (0 if d.scenarios else 1)
                )
                for index in range(count):
                    values = {
                        p.binding: scoped[
                            subject.id
                            if p.scope == "per_building"
                            else subject.group
                            if p.scope == "per_group"
                            else "shared"
                        ][index][p.binding]
                        for p in d.parameters
                        if _selected(p.selector, subject)
                    }
                    for p in _condition_order(d.parameters):
                        if p.binding not in values:
                            continue
                        effective = {
                            canonical_binding(key): value
                            for key, value in values.items()
                        }
                        if not all(
                            (
                                effective[canonical_binding(key)]
                                if canonical_binding(key) in effective
                                else read_binding(base, key)
                            )
                            == expected
                            for key, expected in p.applies_when.items()
                        ):
                            values.pop(p.binding)
                    candidate = apply_bindings(
                        base,
                        {**deferred, **values},
                        selectors={**selectors, **{p.binding: p.selector for p in d.parameters}},
                    )
                    alternatives.append(
                        (f"sample-{index:06d}", candidate, values, False)
                    )
                alternatives.extend(
                    (
                        s.id,
                        apply_bindings(
                            Building.from_dict(s.building), fixed, selectors=selectors
                        ),
                        {},
                        False,
                    )
                    for s in d.scenarios
                    if s.subject == subject.id
                )
            except (ValueError, TypeError) as error:
                diagnostics.append(
                    _diagnostic("invalid_sample", subject.id, str(error))
                )
                continue
            matching = [
                w for w in d.weather if w.subjects is None or subject.id in w.subjects
            ]
            if not matching:
                diagnostics.append(
                    _diagnostic(
                        "missing_weather_assignment",
                        subject.id,
                        "No weather is assigned to this building",
                    )
                )
            for scenario_id, building, values, is_baseline in alternatives:
                for weather in matching:
                    for engine in d.engines:
                        axes = {
                            "building_id": subject.id,
                            "group": subject.group,
                            "scenario_id": scenario_id,
                            "sample_id": scenario_id,
                            "weather_id": weather.id,
                            "engine": engine,
                            "is_baseline": is_baseline,
                            "pair_id": digest([subject.id, scenario_id, weather.id]),
                            "baseline_key": digest([subject.id, weather.id, engine])
                            if d.baseline
                            else None,
                        }
                        for name in (
                            "location_id",
                            "source_year",
                            "climate_scenario",
                            "climate_period",
                        ):
                            if name in weather.metadata:
                                axes["weather_" + name] = weather.metadata[name]
                        entry = {
                            "axes": axes,
                            "parameters": values,
                            "building": building.to_dict(),
                            "weather": weather.weather,
                            "simulation": {
                                "engine": engine,
                                **d.engine_settings.get(engine, {}),
                            },
                            "state_id": building.snapshot().sha256,
                        }
                        if check_ready:
                            from epsm import preflight

                            try:
                                checked = preflight(
                                    building,
                                    weather=Weather.from_dict(weather.weather),
                                    engine=engine,
                                    outputs=[
                                        x.model_dump(mode="json") for x in d.outputs
                                    ],
                                    **d.engine_settings.get(engine, {}),
                                )
                            except (ValueError, TypeError) as error:
                                checked = {
                                    "diagnostics": [
                                        _diagnostic(
                                            "invalid_evaluation_request",
                                            "outputs",
                                            str(error),
                                        )
                                    ]
                                }
                            for issue in checked["diagnostics"]:
                                diagnostics.append({**issue, "axes": axes})
                        rows.append(entry)
        return rows, diagnostics

    def preview(self, *, check_ready=True):
        """Return exact expanded membership and diagnostics without running engines."""
        rows, diagnostics = self._expanded(check_ready=check_ready)
        report = {
            "workflow_id": self.id,
            "ready": not any(
                x.get("severity", "error") == "error" for x in diagnostics
            ),
            "case_count": len(rows),
            "cases": rows,
            "diagnostics": diagnostics,
        }
        event(
            logger,
            "study.preview.completed",
            "Expanded study preview",
            context={"workflow_id": self.id},
            details={"case_count": len(rows), "ready": report["ready"]},
        )
        return report

    def describe_design(self, *, check_ready=True, examples=3):
        """Describe an exact preview without returning full physical models.

        Counts describe evaluations, including repeated weather/engine axes.
        ``design_count`` counts building/scenario pairs. Numeric ranges and
        finite-choice counts describe realized values; missing counts retain
        inapplicable inputs. This is descriptive review, not statistical analysis.
        ``readiness_checked=False`` explicitly means engines were not preflighted.
        """
        from collections import Counter

        if type(examples) is not int or not 0 <= examples <= 20:
            raise ValueError("examples must be an integer from zero to twenty")
        report = self.preview(check_ready=check_ready)
        rows = report["cases"]
        summaries = {}
        for parameter in self._definition.parameters:
            values = [row["parameters"][parameter.binding] for row in rows if parameter.binding in row["parameters"]]
            summary = {"kind": parameter.kind, "unit": parameter.unit,
                       "available": len(values), "missing": len(rows) - len(values)}
            if parameter.choices is not None or parameter.kind == "categorical":
                counts = Counter(json.dumps(value, sort_keys=True, ensure_ascii=False) for value in values)
                indices = {json.dumps(value, sort_keys=True, ensure_ascii=False): index
                           for index, value in enumerate(parameter.choices or [])}
                summary["choices"] = []
                for encoded, count in sorted(counts.items()):
                    value = json.loads(encoded)
                    choice = {"count": count, "choice_index": indices[encoded]}
                    if isinstance(value, (dict, list)):
                        choice["choice_id"] = digest(value)
                    else:
                        choice["value"] = value
                    summary["choices"].append(choice)
            elif values:
                summary.update(min=min(values), max=max(values))
            summaries[parameter.binding] = summary
        return {
            "format": "epsm-design-preview/1", "workflow_id": report["workflow_id"],
            "ready": report["ready"], "readiness_checked": check_ready,
            "case_count": report["case_count"],
            "design_count": len({(row["axes"]["building_id"], row["axes"]["scenario_id"]) for row in rows}),
            "counts": {axis: dict(Counter(str(row["axes"][axis]) for row in rows if row["axes"].get(axis) is not None))
                       for axis in ("building_id", "group", "weather_id", "engine")},
            "parameters": summaries, "fixed_inputs": sorted(self._definition.model_inputs),
            "examples": [{"axes": row["axes"], "parameters": {
                binding: ({"value_id": digest(value), "value_type": "object" if isinstance(value, dict) else "array"}
                          if isinstance(value, (dict, list)) else value)
                for binding, value in row["parameters"].items()
            }, "state_id": row["state_id"]} for row in rows[:examples]],
            "diagnostics": report["diagnostics"],
        }

    def prepare(self, directory):
        """Freeze resolved inputs through the existing schema-3 prepared executor."""
        from .execution import PreparedEvaluation, prepare

        report = self.preview()
        if not report["ready"] or not report["cases"]:
            raise ValueError(
                "Study is not ready: "
                + "; ".join(x["message"] for x in report["diagnostics"])
            )
        cases = []
        outputs = [x.model_dump(mode="json") for x in self._definition.outputs]
        for index, row in enumerate(report["cases"]):
            weather = deepcopy(row["weather"])
            filename = weather["epw_path"]
            weather["epw_path"] = {"$asset": "weather"}
            simulation = {**row["simulation"], "weather": weather}
            cases.append(
                PreparedEvaluation(
                    {
                        "building": row["building"],
                        "simulation": simulation,
                        "result_storage": "bundle",
                        "output_requirements": outputs,
                    },
                    inputs={
                        "axes": row["axes"],
                        "parameters": row["parameters"],
                        "state_id": row["state_id"],
                    },
                    name=f"evaluation-{index:06d}",
                    files={"weather": filename},
                )
            )
        manifest = prepare(directory, cases, environment={"workflow_id": self.id})
        specfile = Path(directory) / "workflow.json"
        if specfile.exists() and json.loads(specfile.read_text()) != self.to_dict():
            raise ValueError("Frozen workflow specification differs")
        atomic_json(specfile, self.to_dict())
        return manifest


def _selected(selector, subject):
    return (not selector.get("subjects") or subject.id in selector["subjects"]) and (
        not selector.get("groups") or subject.group in selector["groups"]
    )


def _embed_schema(schema, definition, field, owner_schema, prefix):
    """Compose an owner JSON Schema with namespaced references in one document."""

    def rebase(value):
        if isinstance(value, dict):
            return {
                key: (
                    "#/$defs/" + prefix + item.rsplit("/", 1)[-1]
                    if key == "$ref"
                    and isinstance(item, str)
                    and item.startswith("#/$defs/")
                    else rebase(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [rebase(item) for item in value]
        return value

    value = deepcopy(owner_schema)
    definitions = value.pop("$defs", {})
    for name, node in definitions.items():
        schema["$defs"][prefix + name] = rebase(node)
    schema["$defs"][definition]["properties"][field].update(rebase(value))


def _condition_order(parameters):
    """Resolve condition dependencies deterministically; reject cycles explicitly."""
    from .bindings import canonical_binding

    by_binding = {
        canonical_binding(parameter.binding): parameter for parameter in parameters
    }
    ordered, visited, visiting = [], set(), set()

    def visit(parameter):
        key = canonical_binding(parameter.binding)
        if key in visited:
            return
        if key in visiting:
            raise ValueError("Parameter applicability conditions contain a cycle")
        visiting.add(key)
        for dependency in parameter.applies_when:
            parent = by_binding.get(canonical_binding(dependency))
            if parent is not None:
                visit(parent)
        visiting.remove(key)
        visited.add(key)
        ordered.append(parameter)

    for parameter in parameters:
        visit(parameter)
    return ordered


def _resolve_weather_batch(row, directory, *, subject_ids):
    """Use the owner's batch manifest and receipts to retain weather case axes."""
    from epsm_core import Weather
    from smhi2epw import MorphConfig, WeatherLocation, compile_amy_batch
    from smhi2epw.io import file_sha256, read_json

    request = row["request"]
    locations = [WeatherLocation(**value) for value in request["locations"]]
    assignments = request.get("subjects_by_location", {})
    if not isinstance(assignments, dict) or not set(assignments) <= {
        location.name for location in locations
    }:
        raise ValueError(
            "Weather subject assignments require known location identifiers"
        )
    for names in assignments.values():
        if not isinstance(names, list) or not names or not set(names) <= subject_ids:
            raise ValueError(
                "Weather location assignments require nonempty known subject lists"
            )
    options = dict(request.get("options", {}))
    if request.get("compiler_options") is not None:
        if "compiler_options" in options:
            raise ValueError("Supply compiler_options once")
        options["compiler_options"] = request["compiler_options"]
    if request.get("morphing"):
        options["morphing"] = [MorphConfig(**value) for value in request["morphing"]]
    result = compile_amy_batch(
        locations, request["years"], output_dir=directory, **options
    )
    manifest = read_json(Path(result.manifest_path))
    if manifest.get("schema_version") != 1 or manifest.get("status") != "completed":
        raise ValueError("Weather batch returned no completed supported owner manifest")
    jobs = manifest.get("jobs", [])
    if len(jobs) != len(result.amy_results) or not request["years"]:
        raise ValueError("Weather batch result and manifest disagree")
    manifest_ref = {
        "path": str(Path(result.manifest_path).resolve()),
        "sha256": file_sha256(Path(result.manifest_path)),
    }
    by_source = {}
    expanded = []

    def entry(path, receipt_path, metadata, identity, selected_subjects):
        receipt = read_json(Path(receipt_path)) if receipt_path else {}
        expected_hash = receipt.get("epw_sha256", receipt.get("output_sha256"))
        provenance = {
            "batch_manifest": manifest_ref,
            "owner_receipt": receipt,
            "batch_selection": metadata,
        }
        if expected_hash is not None:
            provenance["epw_sha256"] = expected_hash
        weather = Weather.from_epw(
            path, calendar_year=metadata["source_year"], provenance=provenance
        )
        weather.validate()
        return {
            **row,
            "id": identity,
            "weather": weather.to_dict(),
            "subjects": selected_subjects,
            "metadata": {
                **row.get("metadata", {}),
                **metadata,
                "batch_manifest": manifest_ref,
                "owner_provenance_path": receipt_path,
            },
        }

    # The public batch contract orders completed jobs by location, then year.
    for index, (job, amy) in enumerate(zip(jobs, result.amy_results)):
        if (
            job.get("status") != "completed"
            or Path(job["amy_path"]).resolve() != Path(amy.output_path).resolve()
        ):
            raise ValueError(
                "Weather batch manifest does not identify its completed AMY"
            )
        location = locations[index // len(request["years"])]
        selected_subjects = assignments.get(location.name, row.get("subjects"))
        metadata = {
            "location_id": location.name,
            "source_year": job["source_year"],
            "classification": "amy",
        }
        stem = f"{row['id']}-{location.name}-{job['source_year']}"
        expanded.append(
            entry(
                amy.output_path,
                job.get("provenance_path"),
                metadata,
                stem + "-amy",
                selected_subjects,
            )
        )
        by_source[str(Path(amy.output_path).resolve())] = (
            metadata,
            stem,
            selected_subjects,
            job,
        )
    for future in result.morph_results:
        source = by_source.get(str(Path(future.source_path).resolve()))
        if source is None:
            raise ValueError("Morphed weather does not reference a batch baseline")
        metadata, stem, selected_subjects, job = source
        declared = {
            str(Path(item["output_path"]).resolve()) for item in job.get("morphed", [])
        }
        if str(Path(future.output_path).resolve()) not in declared:
            raise ValueError("Future weather is absent from the owner's batch manifest")
        metadata = {
            **metadata,
            "classification": "morphed_future_scenario",
            "climate_scenario": future.scenario,
            "climate_period": future.period,
            "source_epw_path": future.source_path,
        }
        expanded.append(
            entry(
                future.output_path,
                future.provenance_path,
                metadata,
                f"{stem}-{future.scenario}-{future.period}",
                selected_subjects,
            )
        )
    return expanded


def _diagnostic(code, field, message):
    return {"code": code, "field": field, "message": message, "severity": "error"}


def template(method="lhs", **kwargs):
    """Public convenience constructor for a workflow template."""
    return WorkflowSpec.template(method, **kwargs)
