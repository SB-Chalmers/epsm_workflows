"""Scientific guardrails for the frozen EPC archetype pilot."""

import json
from collections import Counter
from types import SimpleNamespace

import pytest
from shapely.geometry import Polygon

from epsm_workflows import archetype_pilot as pilot


def raw(**changes):
    row = dict(
        EgenByggnadsKat="Flerbostadshus",
        EgenNybyggAr="1970",
        EgenAtemp="1000",
        EgenAntalPlan="3",
        EgiBeraknatVarde="Nej",
        EgiForstaArManad="1801",
        EgiSistaArManad="1812",
        EgiFjarrvarmeUPPV="100000",
        EgiFjarrvarmeVV="25000",
        VentTypF="Ja",
        VentTypFTX="Nej",
    )
    return row | changes


def test_epc_target_and_accounting_gates():
    observation = pilot._observation(raw())
    assert observation["heat_kwh"] / observation["atemp_m2"] == 100
    assert observation["measurement_year"] == 2018
    for changes, reason in [
        ({"EgiBeraknatVarde": "Ja"}, "calculated_or_unknown_heat"),
        ({"EgiForstaArManad": "1802", "EgiSistaArManad": "1901"}, "not_calendar_year"),
        ({"VentTypFTX": "Ja"}, "ventilation_not_single_f"),
        ({"EgiOljaUPPV": "1"}, "mixed_heat_carriers"),
        ({"EgenAtemp": "0"}, "invalid_area_or_heat"),
    ]:
        with pytest.raises(ValueError, match=reason):
            pilot._observation(raw(**changes))


def test_one_building_link_and_conflicting_certificate_rows():
    link = ("building", "property", "house", "multifamily")
    assert pilot._singleton_link([link]) == link
    with pytest.raises(ValueError, match="ambiguous_link"):
        pilot._singleton_link([link, link])
    with pytest.raises(ValueError, match="missing_property_key"):
        pilot._singleton_link([("building", "", "house", "multifamily")])
    assert pilot._raw_signature(raw()) != pilot._raw_signature(
        raw(EgiFjarrvarmeUPPV="90000")
    )


def test_pitched_footprint_preserves_original_irregular_shape():
    footprint = Polygon([(0, 0), (20, 0), (20, 10), (0, 10)])
    encoded, difference = pilot._model_footprint(footprint.wkb, "pitched_ridge")
    from shapely import from_wkb

    assert from_wkb(bytes.fromhex(encoded)).area == pytest.approx(footprint.area)
    assert difference == pytest.approx(0)
    irregular = Polygon([(0, 0), (20, 0), (20, 5), (10, 5), (10, 10), (0, 10)])
    encoded, difference = pilot._model_footprint(irregular.wkb, "pitched_ridge")
    assert from_wkb(bytes.fromhex(encoded)).equals_exact(irregular, 0)
    assert difference == 0


def test_height_uses_eave_for_pitched_roof():
    pitched = dict(
        height_status="determined",
        roof_form="pitched_ridge",
        representative_height_m=11,
        eave_height_m=9,
        ridge_height_m=12,
    )
    assert pilot._geometry(pitched, 3) == ("pitched_ridge", 3)
    assert pilot._geometry({**pitched, "roof_form": "flat"}, 3)[1] == 11 / 3
    assert pilot._geometry({**pitched, "eave_height_m": 6}, 3)[1] == 2
    for invalid in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="invalid_measured_height"):
            pilot._geometry(
                {**pitched, "roof_form": "flat", "representative_height_m": invalid}, 3
            )
    with pytest.raises(ValueError, match="invalid_pitched_roof_heights"):
        pilot._geometry({**pitched, "ridge_height_m": 8}, 3)
    with pytest.raises(ValueError, match="roof_form_unsupported"):
        pilot._geometry({**pitched, "roof_form": "unclear"}, 3)


def test_sampling_preserves_properties_and_is_energy_blind():
    rows = [
        dict(
            building_id=f"b{i}",
            property_key=f"p{i // 2}",
            atemp_m2=100 + i * 10,
            representative_height_m=9 + i % 5,
            roof_form="flat" if i % 2 else "pitched_ridge",
            measurement_year=2018 + i % 3,
            heat_kwh=i * 1e6,
        )
        for i in range(240)
    ]
    first = pilot._split(rows)
    second = pilot._split([{**r, "heat_kwh": -1} for r in rows])
    assert {k: [r["building_id"] for r in v] for k, v in first.items()} == {
        k: [r["building_id"] for r in v] for k, v in second.items()
    }
    assert sum(map(len, first.values())) == 200
    allocation = {
        r["property_key"]: split for split, group in first.items() for r in group
    }
    assert all(
        all(allocation[r["property_key"]] == split for r in group)
        for split, group in first.items()
    )
    assert Counter(allocation.values())


def test_mixture_training_and_holdout_isolation(monkeypatch, tmp_path):
    train = [
        dict(building_id=f"train{i}", split="train", atemp_m2=1, heat_kwh=35)
        for i in range(118)
    ]
    validation = [dict(building_id="val", split="validation", atemp_m2=1, heat_kwh=35)]
    test = [dict(building_id="test", split="test", atemp_m2=1, heat_kwh=9999)]
    root = tmp_path
    (root / "baseline.json").write_text(
        json.dumps(
            {
                "analysis_building_ids": [r["building_id"] for r in train + validation],
            }
        )
    )
    seen = []

    def fake_predictions(_root, rows, *_args, **kwargs):
        seen.extend(r["building_id"] for r in rows)
        result = {r["building_id"]: [45, 35, 25] for r in rows}
        return (result, []) if kwargs.get("tolerate_failures") else result

    monkeypatch.setattr(
        pilot,
        "_require_study",
        lambda _root: (root, {"selected": 120}, train + validation + test),
    )
    monkeypatch.setattr(pilot, "_weather_manifest", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(pilot, "_runner", lambda *_args: None)
    monkeypatch.setattr(pilot, "_predictions", fake_predictions)
    pilot.calibrate(SimpleNamespace(study=root, weather_dir=root, energyplus="unused"))
    assert "test" not in seen
    result = json.loads((root / "calibration.json").read_text())
    assert result["frozen_before_test"] is True
    assert all(0 <= x <= 1 for x in result["weights"])
    assert sum(result["weights"]) == pytest.approx(1)


def test_prepare_weather_preserves_compiler_artifact_and_provenance(
    monkeypatch, tmp_path
):
    from epsm_workflows import weather

    monkeypatch.setattr(
        pilot,
        "_require_study",
        lambda _root: (tmp_path, {}, [{"measurement_year": 2020}]),
    )
    content = "compiler-owned leap weather\n"
    provenance = {"weather_sha256": "compiler hash", "calendar": {"year": 2020}}

    def compile_weather(_start, _end, directory, **_kwargs):
        directory.mkdir(parents=True)
        (directory / "weather-2020.epw").write_text(content)
        return None, provenance.copy()

    monkeypatch.setattr(weather, "compile_measurement_weather", compile_weather)
    args = SimpleNamespace(study=tmp_path, output=tmp_path / "weather", station=[])
    pilot.prepare_weather(args)
    assert (args.output / "weather-2020.epw").read_text() == content
    assert (
        json.loads((args.output / "weather-2020-provenance.json").read_text())
        == provenance
    )
    # Existing pinned artifacts must not be rewritten or recompiled.
    monkeypatch.setattr(
        weather,
        "compile_measurement_weather",
        lambda *_a, **_kw: pytest.fail("existing weather must be reused"),
    )
    pilot.prepare_weather(args)
    assert (args.output / "weather-2020.epw").read_text() == content


def test_variant_failure_is_recorded_without_using_epc_heat(monkeypatch, tmp_path):
    rows = [
        dict(building_id="good", atemp_m2=100, heat_kwh=9000),
        dict(building_id="bad", atemp_m2=100, heat_kwh=999999),
    ]

    def fake_run(_root, row, state, *_args, **_kwargs):
        if row["building_id"] == "bad" and state == "advanced":
            raise RuntimeError("strict numerical warning")
        return 5000

    monkeypatch.setattr(pilot, "_run_variant", fake_run)
    predicted, failures = pilot._predictions(
        tmp_path, rows, tmp_path, None, tolerate_failures=True
    )
    assert set(predicted) == {"good"}
    assert [(f["building_id"], f["state"]) for f in failures] == [("bad", "advanced")]


def test_fitted_role_preserves_material_identity():
    from epsm_core import ConstructionSpec, LayerSpec, MaterialSpec

    dense = MaterialSpec(
        name="dense",
        conductivity_w_mk=1.0,
        density_kg_m3=1000,
        specific_heat_j_kgk=1000,
    )
    insulation = MaterialSpec(
        name="insulation",
        conductivity_w_mk=0.04,
        density_kg_m3=30,
        specific_heat_j_kgk=1000,
    )
    original = ConstructionSpec(
        layers=(
            LayerSpec(material=dense, thickness_m=0.1),
            LayerSpec(material=insulation, thickness_m=0.2),
        )
    )
    changed = pilot._insulation_scaled(original, 1.2)
    assert changed.layers[0] == original.layers[0]
    assert changed.layers[1].material == original.layers[1].material
    assert changed.layers[1].thickness_m == pytest.approx(0.24)
