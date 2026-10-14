-- Group roles have no passwords/logins. Provision login membership separately.
-- Apply as the schema owner on the dedicated empty database.
BEGIN;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'epsm_reader') THEN
    CREATE ROLE epsm_reader NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'epsm_importer') THEN
    CREATE ROLE epsm_importer NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'epsm_worker') THEN
    CREATE ROLE epsm_worker NOLOGIN;
  END IF;
END $$;

CREATE VIEW api.building_evidence AS
SELECT l.*, r.release_tag, a.uri AS source_asset_uri,
       a.content_sha256 AS source_asset_sha256,
       p.policy_code AS method_policy_code, p.version AS method_policy_version
FROM stock.field_lineage l
JOIN stock.snapshot s USING (snapshot_id)
LEFT JOIN meta.source_release r ON r.source_release_id = l.source_release_id
LEFT JOIN meta.source_asset a ON a.source_asset_id = l.source_asset_id
LEFT JOIN meta.policy_version p ON p.policy_version_id = l.method_policy_id
WHERE s.status = 'published';
CREATE VIEW api.source_releases AS
SELECT r.*, s.source_code, s.publisher, s.description, s.source_uri, s.licence
FROM meta.source_release r JOIN meta.source s USING (source_id)
WHERE r.status = 'published';
CREATE VIEW api.context AS
SELECT o.*, a.name AS area_name
FROM geo.context_observation o
JOIN geo.area a ON (a.source_release_id, a.area_level, a.area_code) =
                   (o.area_release_id, o.area_level, o.area_code)
JOIN meta.source_release r ON r.source_release_id = o.source_release_id
WHERE r.status = 'published';
CREATE VIEW api.epc_building_links AS
SELECT p.snapshot_id, l.*
FROM epc.certificate_building_link l
JOIN (SELECT DISTINCT snapshot_id, source_release_id FROM stock.snapshot_source) p
  ON p.source_release_id = l.source_release_id
JOIN stock.snapshot s ON s.snapshot_id = p.snapshot_id
WHERE s.status = 'published';
CREATE VIEW api.building_area_assignments AS
SELECT a.* FROM stock.building_area_assignment a
JOIN stock.snapshot s USING(snapshot_id) WHERE s.status = 'published';
CREATE VIEW api.simulation_results AS
SELECT r.building_id, r.snapshot_id, r.scenario_id, r.catalogue_release_id,
       r.model_revision_id, r.spec_sha256, r.physical_sha256,
       run.simulation_run_id, run.status, run.actual_core_run_id,
       run.started_at, run.completed_at, run.annual_scalars, run.failure_detail
FROM model.revision r JOIN model.simulation_run run USING(model_revision_id);

GRANT USAGE ON SCHEMA api TO epsm_reader, epsm_importer, epsm_worker;
GRANT SELECT ON ALL TABLES IN SCHEMA api TO epsm_reader, epsm_importer, epsm_worker;
GRANT USAGE ON SCHEMA meta, geo, stock, epc, library, qa TO epsm_importer;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA meta, geo, stock, epc, library, qa TO epsm_importer;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA meta, geo, stock, epc, library, qa TO epsm_importer;
GRANT USAGE ON SCHEMA meta, library, model TO epsm_worker;
GRANT SELECT ON ALL TABLES IN SCHEMA meta, library TO epsm_worker;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA model TO epsm_worker;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA model TO epsm_worker;

-- Future API views should be readable after migrations made by this owner.
ALTER DEFAULT PRIVILEGES IN SCHEMA api GRANT SELECT ON TABLES TO epsm_reader, epsm_importer, epsm_worker;
COMMIT;
