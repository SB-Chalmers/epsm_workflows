"""Bounded exploration of physical renovation trees with two explicit objectives.

Every edge applies an action bundle to its parent's building. Intermediate
states remain candidates; dominated prefixes are never pruned. Scientific
energy and carbon calculations belong to the supplied EPSM evaluator.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
import math
from pathlib import Path

from .records import atomic_json, digest, exclusive_lock, json_value, read_json


@dataclass(frozen=True)
class RenovationNode:
    """One physical state reached by one ordered intervention path."""

    id: str
    parent_id: str | None
    depth: int
    action_ids: tuple[str, ...]
    bundle: tuple[dict, ...]
    building: dict
    evaluation: dict
    energy_kwh: float | None
    carbon_kgco2e: float | None
    feasible: bool
    reasons: tuple[str, ...] = ()

    @property
    def evaluation_id(self):
        return self.evaluation.get("id")

    @property
    def energy_id(self):
        return self.evaluation.get("energy_id")

    @property
    def lca_id(self):
        return self.evaluation.get("lca_id")

    @classmethod
    def from_dict(cls, value):
        data = dict(value)
        for key in ("action_ids", "bundle", "reasons"):
            data[key] = tuple(data[key])
        return cls(**data)


@dataclass(frozen=True)
class RenovationTree:
    """All evaluated nodes, rejected transitions and feasible Pareto alternatives."""

    id: str
    nodes: tuple[RenovationNode, ...]
    rejected: tuple[dict, ...] = ()
    exhausted_budget: bool = False

    @property
    def pareto(self):
        feasible = [node for node in self.nodes if node.feasible]
        return tuple(node for node in feasible if not any(
            other.energy_kwh <= node.energy_kwh and other.carbon_kgco2e <= node.carbon_kgco2e
            and (other.energy_kwh < node.energy_kwh or other.carbon_kgco2e < node.carbon_kgco2e)
            for other in feasible if other.id != node.id
        ))

    def children(self, node):
        identifier = node.id if isinstance(node, RenovationNode) else node
        return tuple(child for child in self.nodes if child.parent_id == identifier)

    def to_dict(self):
        from dataclasses import asdict
        return asdict(self)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _assessment(evaluation, constraints):
    data = json_value(evaluation)
    energy, carbon = data.get("energy") or {}, data.get("lca") or {}
    energy_object = getattr(evaluation, "energy", energy)
    carbon_object = getattr(evaluation, "lca", carbon)
    def field(value, name, default=None):
        return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)
    e = _number(field(energy_object, "annual_delivered_energy_kwh"))
    c = _number(field(carbon_object, "total_gwp_kgco2e"))
    reasons = []
    if e is None:
        reasons.append("missing annual delivered energy objective")
    elif e < 0:
        reasons.append("annual delivered energy cannot be negative")
    if c is None:
        reasons.append("missing life-cycle carbon objective")
    if (field(evaluation, "complete") is False or field(energy_object, "complete") is False
            or field(carbon_object, "complete") is False
            or field(energy_object, "requirements") or field(carbon_object, "requirements")
            or field(carbon_object, "status", "complete") != "complete"
            or field(energy_object, "diagnostics", {}).get("accepted") is False):
        reasons.append("incomplete evaluation")
    for index, constraint in enumerate(constraints):
        verdict = constraint(evaluation)
        if verdict is not True:
            reasons.append(str(verdict) if isinstance(verdict, str) else f"constraint {index} failed")
    return data, e, c, tuple(reasons)


def optimise_renovations(
    building, catalogue=None, *, bundles=None, max_depth=1, budget=100,
    max_bundle_size=1, allow_repeated=False, evaluator=None, evaluator_id=None,
    engine="energyplus", weather=None, assessment=None, cache=None,
    constraints=(), constraint_id=None, directory=None, evaluation_options=None,
):
    """Explore a renovation tree in deterministic breadth-first order.

    Parameters
    ----------
    building
        EPSM Building exposing to_dict/from_dict and renovate(*measures).
    catalogue
        RenovationCatalogue. Executable measures form finite combinations up to
        max_bundle_size; supply bundles to select explicit resolved alternatives.
    bundles
        Iterable of measure tuples. Each tuple is one atomic edge, not a stage slot.
    max_depth, budget : int
        Maximum successive bundles and total evaluated nodes, including baseline.
        Increasing either value resumes an existing directory without repeating
        completed evaluations. Rejected transitions do not consume evaluation budget.
    evaluator
        Optional callable(building) returning an EPSM evaluation. Persistent runs
        require evaluator_id so changing evaluator semantics cannot reuse old results.
    constraints
        Callables returning True, False or a failure explanation. They control
        feasibility, not expansion. Persistent runs require constraint_id.
    directory
        Optional checkpoint directory. A single writer owns a tree. Energy/LCA
        caching remains in the EPSM evaluator and uses independent identities.

    Returns
    -------
    RenovationTree
        Evaluated intermediate states and feasible nondominated alternatives.
    """
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 0:
        raise ValueError("max_depth must be a nonnegative integer")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise ValueError("budget must include at least the baseline evaluation")
    if not isinstance(max_bundle_size, int) or max_bundle_size < 1:
        raise ValueError("max_bundle_size must be positive")
    if directory is not None and evaluator is not None and not evaluator_id:
        raise ValueError("Persistent custom evaluators require evaluator_id")
    if directory is not None and constraints and not constraint_id:
        raise ValueError("Persistent constraints require constraint_id")
    if bundles is None:
        if catalogue is None:
            raise ValueError("Supply a catalogue or resolved measure bundles")
        measures = tuple(m for m in catalogue.filter() if getattr(m, "is_executable", True))
        bundles = [bundle for size in range(1, min(max_bundle_size, len(measures)) + 1)
                   for bundle in combinations(measures, size)]
    bundles = tuple(tuple(bundle) for bundle in bundles)
    if any(not bundle for bundle in bundles):
        raise ValueError("Renovation bundles cannot be empty")
    definitions = [json_value(bundle) for bundle in bundles]
    if len({digest(bundle) for bundle in definitions}) != len(bundles):
        raise ValueError("Duplicate renovation bundle")
    for bundle in bundles:
        if len({m.measure_id for m in bundle}) != len(bundle):
            raise ValueError("A bundle cannot contain the same measure twice")
    options = dict(evaluation_options or {})
    output_root = options.pop("directory", Path(directory) / "artifacts" if directory else None)
    if evaluator is None:
        # Search owns an independent working baseline; caller edits and results
        # remain separate from branch execution.
        building = building.copy()
        if assessment is not None:
            from copy import deepcopy
            building.assessment = deepcopy(assessment)
        if cache is None:
            from epsm import EvaluationCache
            cache = EvaluationCache(Path(directory) / "cache" if directory else None)
    evaluation_config = {"evaluator_id": evaluator_id} if evaluator else {
        "engine": engine, "weather": json_value(weather), "options": json_value(options)}
    config = {"schema": 1, "baseline": json_value(building), "bundles": definitions,
              "allow_repeated": allow_repeated, "evaluation": evaluation_config,
              "constraint_id": constraint_id}
    identity = digest(config)
    if evaluator is None:
        def evaluator(candidate):
            run_options = dict(options)
            if output_root is not None:
                run_options["directory"] = Path(output_root) / digest(json_value(candidate))
            return candidate.simulate(engine=engine, weather=weather, cache=cache, **run_options)

    def explore():
        checkpoint = Path(directory) / "tree.json" if directory is not None else None
        nodes, rejected = [], []
        if checkpoint and checkpoint.exists():
            stored = read_json(checkpoint)
            checksum = stored.pop("checksum", None)
            if checksum != digest(stored):
                raise ValueError("Search checkpoint content has changed or is incomplete")
            if stored["id"] != identity:
                raise ValueError("Search configuration differs from the stored tree")
            nodes = [RenovationNode.from_dict(n) for n in stored["nodes"]]
            rejected = stored["rejected"]
        known = {node.id: node for node in nodes}
        rejected_ids = {entry["id"] for entry in rejected}
        live = {}

        def save():
            if checkpoint:
                stored = {"id": identity, "configuration": config, "nodes": nodes, "rejected": rejected}
                atomic_json(checkpoint, {**stored, "checksum": digest(stored)})

        def evaluate(candidate, identifier, parent, bundle):
            try:
                evaluation = evaluator(candidate)
                data, energy, carbon, reasons = _assessment(evaluation, constraints)
            except Exception as error:
                # Keep failure evidence and expand physically valid states; one
                # unsupported state need not make all descendants unsupported.
                data = {"id": None, "failure": type(error).__name__, "message": str(error)}
                energy = carbon = None
                reasons = (f"evaluation failed: {error}",)
            node = RenovationNode(
                identifier, parent.id if parent else None, parent.depth + 1 if parent else 0,
                (parent.action_ids if parent else ()) + tuple(m.measure_id for m in bundle),
                tuple(json_value(bundle)), json_value(candidate), data, energy, carbon,
                not reasons, reasons,
            )
            nodes.append(node)
            known[identifier] = node
            live[identifier] = candidate
            save()
            return node

        root_id = digest({"search": identity, "root": config["baseline"]})
        if not nodes:
            evaluate(building, root_id, None, ())
        live[root_id] = building
        cursor = 0
        exhausted = False
        while cursor < len(nodes):
            parent = nodes[cursor]
            cursor += 1
            if parent.depth >= max_depth:
                continue
            if parent.id not in live:
                live[parent.id] = type(building).from_dict(parent.building)
            for bundle, definition in zip(bundles, definitions):
                if not allow_repeated and any(m.measure_id in parent.action_ids for m in bundle):
                    continue
                identifier = digest({"parent": parent.id, "bundle": definition})
                if identifier in known or identifier in rejected_ids:
                    continue
                if len(nodes) >= budget:
                    exhausted = True
                    break
                try:
                    candidate = live[parent.id].renovate(*bundle)
                except (ValueError, TypeError, KeyError) as error:
                    rejected.append({"id": identifier, "parent_id": parent.id,
                                     "bundle": definition, "reason": str(error)})
                    rejected_ids.add(identifier)
                    save()
                    continue
                evaluate(candidate, identifier, parent, bundle)
            if exhausted:
                break
        return RenovationTree(identity, tuple(nodes), tuple(rejected), exhausted)

    if directory is None:
        return explore()
    with exclusive_lock(Path(directory) / ".tree.lock"):
        return explore()
