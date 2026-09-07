import json
from collections import Counter
from pathlib import Path

import numpy as np
from epsm_workflows.campaign import generate, validate_pairs


def test_design(tmp_path):
    config = json.loads((Path(__file__).parents[1] / "campaigns/buildings.json").read_text())
    for city in config["cities"]:
        weather = tmp_path / f"{city['slug']}.epw"
        weather.write_text(
            f"LOCATION,{city['city']},Test,SWE,TEST,{city['wmo']},"
            f"{city['lat']},{city['lon']},1,20\n"
        )
        city["epw"] = str(weather)
    cases = generate(config)
    validate_pairs(cases)
    assert len(cases) == 10000
    assert all(c.spec.geometry.zoning.mode == "whole_building" for c in cases)
    for case in cases[:200]:
        facade_ids = {f.facade_id for f in case.spec.geometry.facades()}
        assert {f.facade_id for f in case.spec.facade.overrides} <= facade_ids
    assert len({c.city for c in cases[:200]}) == 5
    for city in config["cities"]:
        pairs = [c for c in cases if c.city == city["city"] and c.variant == "a"]
        assert Counter(c.comparison_type for c in pairs) == {
            "shape": 380,
            "orientation": 285,
            "massing": 285,
        }
        assert Counter(c.spec.geometry.roof.topology for c in pairs) == {
            "compact": 475,
            "cold_attic": 475,
        }
        counts = Counter(c.spec.airflow.topology for c in pairs)
        assert max(counts.values()) - min(counts.values()) <= 1
        units = np.asarray([c.requested["lhs_unit"] for c in pairs])
        assert all(len(set((units[:, i] * 950).astype(int))) == 950 for i in range(25))
