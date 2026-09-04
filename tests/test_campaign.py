import json
from pathlib import Path
from collections import Counter

import numpy as np

from epsm_workflows.campaign import generate, validate_pairs


def test_design():
    config = json.loads((Path(__file__).parents[1] / "campaigns/v2_10000.json").read_text())
    cases = generate(config)
    validate_pairs(cases)
    assert len(cases) == 10000
    assert len({c.city for c in cases[:200]}) == 5
    for city in config["cities"]:
        pairs = [c for c in cases if c.city == city["city"] and c.variant == "a"]
        assert Counter(c.comparison_type for c in pairs) == {
            "shape": 380,
            "orientation": 285,
            "massing": 285,
        }
        assert Counter(c.spec.envelope.roof_topology for c in pairs) == {
            "compact": 475,
            "cold_attic": 475,
        }
        counts = Counter(c.spec.airflow.topology for c in pairs)
        assert max(counts.values()) - min(counts.values()) <= 1
        units = np.asarray([c.requested["lhs_unit"] for c in pairs])
        assert all(len(set((units[:, i] * 950).astype(int))) == 950 for i in range(25))
