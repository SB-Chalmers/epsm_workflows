"""Pinned local physical materials and explicit campaign assemblies."""

import json
from importlib.resources import files

from epsm_core import ConstructionSpec, EnvelopeSpec, LayerSpec, MaterialSpec
from epsm_core.constructions import solve_insulation


def materials():
    return {
        k: MaterialSpec.model_validate(v)
        for k, v in json.loads(
            files("epsm_workflows").joinpath("data/materials.json").read_text()
        ).items()
    }


def fabric(wall_m=0.1, roof_m=0.2, floor_m=0.08, *, uplift=0.25):
    m = materials()

    def assembly(parts, films=0.17):
        return ConstructionSpec(
            layers=tuple(LayerSpec(material=m[k], thickness_m=t) for k, t in parts),
            surface_resistance_m2k_w=films,
        )

    slab = assembly([("conc_floor", 0.2)])
    return EnvelopeSpec(
        wall=assembly(
            [("concrete", 0.08), ("mw_old", wall_m), ("concrete", 0.15), ("gypsum", 0.013)]
        ),
        top=assembly([("mw_old", roof_m), ("conc_floor", 0.15)], 0.14),
        ground=assembly(
            [("conc_floor", 0.16), ("eps", floor_m), ("dry_sand", 0.05), ("conc_floor", 0.05)]
        ),
        intermediate_floor=slab,
        weather_shell=assembly([("roof_felt", 0.006), ("conc_roof", 0.15)]),
        basement_wall=assembly([("conc_bsmt", 0.2), ("mw_old", 0.085)]),
        thermal_bridge_uplift_fraction=uplift,
    )


def fabric_from_u(wall, top, ground, roof, uplift):
    base = fabric(uplift=uplift)
    if roof == "compact":
        base = base.updated(
            top=ConstructionSpec(
                layers=(
                    base.weather_shell.layers[0],
                    base.top.layers[0],
                    base.weather_shell.layers[1],
                ),
                surface_resistance_m2k_w=0.14,
            )
        )
    return base.updated(
        wall=solve_insulation(base.wall, 1, wall),
        top=solve_insulation(base.top, 0 if roof == "cold_attic" else 1, top),
        ground=solve_insulation(base.ground, 1, ground),
    )
