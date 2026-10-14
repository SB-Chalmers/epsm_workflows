-- These examples read the published default snapshot. Papers should explicitly
-- filter api.buildings_all_snapshots by a pinned snapshot_id instead.

-- 1. Multifamily buildings in Gothenburg, with readable physical variables.
SELECT building_id, municipality_code, construction_year,
       height_representative_m, storeys_above_ground,
       heated_floor_area_atemp_m2, ventilation_system_code
FROM api.buildings
WHERE municipality_code = '1480' AND primary_use_code = 'multi_family'
ORDER BY building_id
LIMIT 100;

-- 2. A value's source and selection evidence. Replace LIMIT 1's building with
--    the desired UUID in a WHERE building_id = '...'::uuid filter.
SELECT building_id, variable_code, field_status, evidence_origin,
       release_tag, source_asset_uri, source_row_key,
       method_policy_code, method_policy_version, alternatives
FROM api.building_evidence e
WHERE EXISTS (SELECT FROM api.buildings b WHERE (b.snapshot_id,b.building_id) = (e.snapshot_id,e.building_id))
  AND e.building_id IN (SELECT building_id FROM api.buildings ORDER BY building_id LIMIT 1)
ORDER BY variable_code;

-- 3. Direct national counts preserve missing source fields. Resolved availability
--    and observed/derived support are distinct; missing lineage is unassessed.
SELECT b.primary_use_code, count(*) AS footprint_buildings,
       count(b.construction_year) AS resolved_year_buildings,
       count(b.construction_year) FILTER (
         WHERE y.evidence_origin IN ('reported','measured','derived')
           AND y.field_status IN ('present','conflicting')
       ) AS source_supported_year_buildings,
       count(*) FILTER (WHERE y.field_status = 'conflicting') AS year_conflicts,
       count(*) FILTER (WHERE y.variable_code IS NULL) AS year_lineage_unassessed,
       count(*) FILTER (WHERE b.construction_year IS NOT NULL
                         AND b.height_representative_m IS NOT NULL
                         AND b.storeys_above_ground IS NOT NULL
                         AND b.heated_floor_area_atemp_m2 IS NOT NULL) AS four_input_buildings
FROM api.buildings b
LEFT JOIN api.building_evidence y ON (y.snapshot_id, y.building_id) = (b.snapshot_id, b.building_id)
                                    AND y.variable_code = 'construction_year'
GROUP BY b.primary_use_code
ORDER BY b.primary_use_code;

-- 4. EPC link presence uses EXISTS so multiple certificates cannot duplicate
--    buildings. This describes spatial acceptance, not resolved energy scope.
SELECT b.primary_use_code, count(*) AS footprint_buildings,
       count(*) FILTER (WHERE EXISTS (
         SELECT 1 FROM api.epc_building_links l
         WHERE (l.snapshot_id,l.building_id) = (b.snapshot_id,b.building_id) AND l.decision = 'accepted'
       )) AS accepted_epc_linked_buildings
FROM api.buildings b GROUP BY b.primary_use_code ORDER BY b.primary_use_code;

-- 5. Population context through versioned area assignments. Filter a specific
--    source release when several context deliveries have the same year.
SELECT b.building_id, a.area_code AS population_grid_code,
       c.reference_year, c.value_numeric AS population_total,
       c.unit_code, c.source_release_id AS context_source_release_id
FROM api.buildings b
JOIN api.building_area_assignments a ON (a.snapshot_id, a.building_id) = (b.snapshot_id, b.building_id)
                                      AND a.area_level = 'population_grid_1km'
JOIN api.context c ON (c.area_release_id, c.area_level, c.area_code) =
                      (a.area_release_id, a.area_level, a.area_code)
                  AND c.variable_code = 'population_total' AND c.reference_year = 2025
ORDER BY b.building_id LIMIT 100;

-- 6. Published coverage retains its original unit and population definition.
SELECT report_id, scope_code, scope_version, counting_unit,
       use_code, municipality_code, variable_code, metric_code,
       numerator, denominator, 100*fraction AS percent,
       unknown_denominator_reason
FROM api.coverage ORDER BY report_id, scope_code, use_code, municipality_code;

-- 7. Frozen results for a model/scenario; failed attempts remain in the table.
SELECT building_id, snapshot_id, scenario_id, catalogue_release_id,
       spec_sha256, physical_sha256,
       simulation_run_id, status, actual_core_run_id, annual_scalars, failure_detail
FROM api.simulation_results ORDER BY started_at DESC NULLS LAST LIMIT 100;

-- With 020_spatial.sql installed, spatial queries use api.buildings_spatial:
-- SELECT building_id FROM api.buildings_spatial
-- WHERE ST_Intersects(footprint_geometry,
--   ST_MakeEnvelope(315000,6395000,330000,6405000,3006));
