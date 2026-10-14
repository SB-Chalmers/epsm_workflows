-- PostgreSQL >= 15. Spatial columns and indexes are installed by 020_spatial.sql.
-- Source data, published snapshots and finalized specs are append-only.
BEGIN;
CREATE SCHEMA IF NOT EXISTS meta;
CREATE SCHEMA IF NOT EXISTS geo;
CREATE SCHEMA IF NOT EXISTS stock;
CREATE SCHEMA IF NOT EXISTS epc;
CREATE SCHEMA IF NOT EXISTS library;
CREATE SCHEMA IF NOT EXISTS model;
CREATE SCHEMA IF NOT EXISTS qa;
CREATE SCHEMA IF NOT EXISTS api;

CREATE TABLE meta.source (
  source_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_code text NOT NULL UNIQUE,
  publisher text NOT NULL,
  description text NOT NULL,
  source_uri text,
  licence text
);
CREATE TABLE meta.source_release (
  source_release_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id uuid NOT NULL REFERENCES meta.source,
  release_tag text NOT NULL,
  reference_date date,
  imported_at timestamptz NOT NULL DEFAULT now(),
  status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published')),
  manifest jsonb NOT NULL CHECK (jsonb_typeof(manifest) = 'object'),
  manifest_sha256 text CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
  parser_version text,
  UNIQUE (source_id, release_tag)
);
CREATE TABLE meta.source_asset (
  source_asset_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_release_id uuid NOT NULL REFERENCES meta.source_release,
  asset_role text NOT NULL,
  uri text NOT NULL,
  content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  size_bytes bigint CHECK (size_bytes >= 0),
  record_count bigint CHECK (record_count >= 0),
  media_type text,
  metadata jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(metadata) = 'object'),
  UNIQUE (source_release_id, uri),
  UNIQUE (source_release_id, source_asset_id)
);
CREATE TABLE meta.variable (
  variable_code text PRIMARY KEY,
  label text NOT NULL,
  definition text NOT NULL,
  value_type text NOT NULL CHECK (value_type IN ('number','integer','text','boolean','date','geometry','json')),
  unit_code text NOT NULL,
  reference_basis text NOT NULL,
  requires_price_year boolean NOT NULL DEFAULT false,
  dictionary_version text NOT NULL,
  UNIQUE (variable_code, unit_code)
);
CREATE TABLE meta.source_variable (
  source_release_id uuid NOT NULL REFERENCES meta.source_release,
  source_schema text NOT NULL,
  source_field text NOT NULL,
  variable_code text NOT NULL REFERENCES meta.variable,
  source_unit_code text,
  transformation_note text NOT NULL,
  PRIMARY KEY (source_release_id, source_schema, source_field, variable_code)
);
CREATE TABLE meta.policy_version (
  policy_version_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  policy_code text NOT NULL,
  version text NOT NULL,
  definition jsonb NOT NULL CHECK (jsonb_typeof(definition) = 'object'),
  content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  code_asset_id uuid REFERENCES meta.source_asset(source_asset_id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (policy_code, version)
);

CREATE TABLE geo.area (
  source_release_id uuid NOT NULL REFERENCES meta.source_release,
  area_level text NOT NULL,
  area_code text NOT NULL,
  name text NOT NULL,
  parent_area_code text,
  metadata jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(metadata) = 'object'),
  PRIMARY KEY (source_release_id, area_level, area_code)
);
CREATE TABLE geo.context_observation (
  context_observation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_release_id uuid NOT NULL REFERENCES meta.source_release,
  source_asset_id uuid,
  source_row_key text,
  area_release_id uuid NOT NULL,
  area_level text NOT NULL,
  area_code text NOT NULL,
  variable_code text NOT NULL,
  reference_year integer NOT NULL CHECK (reference_year BETWEEN 1 AND 9999),
  unit_code text NOT NULL,
  reference_basis text NOT NULL,
  price_year integer CHECK (price_year BETWEEN 1 AND 9999),
  value_numeric double precision,
  value_text text,
  value_boolean boolean,
  evidence_origin text NOT NULL CHECK (evidence_origin IN ('reported','measured','derived','imputed','assumed')),
  method_policy_id uuid REFERENCES meta.policy_version(policy_version_id),
  evidence jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(evidence) = 'object'),
  CHECK (num_nonnulls(value_numeric, value_text, value_boolean) = 1),
  CHECK (value_numeric IS NULL OR (value_numeric > '-Infinity'::float8 AND value_numeric < 'Infinity'::float8)),
  FOREIGN KEY (variable_code, unit_code) REFERENCES meta.variable(variable_code, unit_code),
  FOREIGN KEY (area_release_id, area_level, area_code) REFERENCES geo.area,
  FOREIGN KEY (source_release_id, source_asset_id) REFERENCES meta.source_asset(source_release_id, source_asset_id)
);

CREATE TABLE stock.building (
  building_id uuid PRIMARY KEY,
  identity_namespace text NOT NULL,
  identity_note text
);
COMMENT ON TABLE stock.building IS 'Canonical physical IDs: preserve the source building UUID; never substitute EPC or property identity.';
CREATE TABLE stock.footprint (
  source_release_id uuid NOT NULL REFERENCES meta.source_release,
  building_id uuid NOT NULL REFERENCES stock.building,
  source_asset_id uuid,
  source_row_key text NOT NULL,
  geometry_sha256 text NOT NULL CHECK (geometry_sha256 ~ '^[0-9a-f]{64}$'),
  outline_kind text,
  geometry_status text NOT NULL CHECK (geometry_status IN ('valid','invalid','missing','unreviewed')),
  area_m2 double precision CHECK (area_m2 > 0 AND area_m2 < 'Infinity'::float8),
  source_crs text NOT NULL,
  evidence jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(evidence) = 'object'),
  PRIMARY KEY (source_release_id, building_id),
  FOREIGN KEY (source_release_id, source_asset_id) REFERENCES meta.source_asset(source_release_id, source_asset_id)
);
CREATE TABLE stock.snapshot (
  snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  snapshot_code text NOT NULL,
  version text NOT NULL,
  as_of_date date NOT NULL,
  status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published')),
  full_physical_inventory boolean NOT NULL DEFAULT true,
  expected_building_count bigint CHECK (expected_building_count >= 0),
  selection_policy_id uuid NOT NULL REFERENCES meta.policy_version(policy_version_id),
  manifest jsonb NOT NULL CHECK (jsonb_typeof(manifest) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (snapshot_code, version)
);
CREATE TABLE stock.snapshot_source (
  snapshot_id uuid NOT NULL REFERENCES stock.snapshot,
  source_release_id uuid NOT NULL REFERENCES meta.source_release,
  source_role text NOT NULL,
  PRIMARY KEY (snapshot_id, source_role, source_release_id)
);
CREATE TABLE stock.building_snapshot (
  snapshot_id uuid NOT NULL REFERENCES stock.snapshot,
  building_id uuid NOT NULL REFERENCES stock.building,
  footprint_release_id uuid NOT NULL,
  municipality_code text,
  county_code text,
  primary_use_code text,
  epc_use_code text,
  construction_year integer CHECK (construction_year BETWEEN 1 AND 9999),
  height_representative_m double precision CHECK (height_representative_m > 0 AND height_representative_m < 'Infinity'::float8),
  height_eave_m double precision CHECK (height_eave_m > 0 AND height_eave_m < 'Infinity'::float8),
  height_ridge_m double precision CHECK (height_ridge_m > 0 AND height_ridge_m < 'Infinity'::float8),
  roof_form_code text,
  storeys_above_ground integer CHECK (storeys_above_ground > 0),
  heated_basement_storeys integer CHECK (heated_basement_storeys >= 0),
  heated_floor_area_atemp_m2 double precision CHECK (heated_floor_area_atemp_m2 > 0 AND heated_floor_area_atemp_m2 < 'Infinity'::float8),
  footprint_area_m2 double precision CHECK (footprint_area_m2 > 0 AND footprint_area_m2 < 'Infinity'::float8),
  ventilation_system_code text,
  ventilation_design_flow_l_s_m2 double precision CHECK (ventilation_design_flow_l_s_m2 >= 0 AND ventilation_design_flow_l_s_m2 < 'Infinity'::float8),
  heating_systems jsonb CHECK (heating_systems IS NULL OR jsonb_typeof(heating_systems) = 'array'),
  qa_flags text[] NOT NULL DEFAULT '{}',
  PRIMARY KEY (snapshot_id, building_id),
  FOREIGN KEY (footprint_release_id, building_id) REFERENCES stock.footprint
);
COMMENT ON COLUMN stock.building_snapshot.primary_use_code IS 'GIS-led physical stock classification; unknown stays NULL and remains in the inventory.';
COMMENT ON COLUMN stock.building_snapshot.epc_use_code IS 'Separately selected reported heated programme; never silently replaces GIS purpose.';
COMMENT ON COLUMN stock.building_snapshot.heated_floor_area_atemp_m2 IS 'Building-scoped heated load-reference area. Certificate totals require an explicit scope/allocation policy.';
COMMENT ON COLUMN stock.building_snapshot.heated_basement_storeys IS 'Zero means no reported heated basement storeys, not absence of an unheated basement.';
COMMENT ON COLUMN stock.building_snapshot.footprint_area_m2 IS 'Selected/cache footprint area, independent of Atemp; selected geometry lives once in stock.footprint.';
CREATE TABLE stock.building_area_assignment (
  snapshot_id uuid NOT NULL,
  building_id uuid NOT NULL,
  area_release_id uuid NOT NULL,
  area_level text NOT NULL,
  area_code text NOT NULL,
  assignment_policy_id uuid NOT NULL REFERENCES meta.policy_version(policy_version_id),
  PRIMARY KEY (snapshot_id, building_id, area_level),
  FOREIGN KEY (snapshot_id, building_id) REFERENCES stock.building_snapshot,
  FOREIGN KEY (area_release_id, area_level, area_code) REFERENCES geo.area
);
CREATE TABLE stock.field_lineage (
  snapshot_id uuid NOT NULL,
  building_id uuid NOT NULL,
  variable_code text NOT NULL REFERENCES meta.variable,
  field_status text NOT NULL CHECK (field_status IN ('present','missing','conflicting','outside_scope','not_applicable')),
  evidence_origin text CHECK (evidence_origin IN ('reported','measured','derived','imputed','assumed')),
  source_release_id uuid REFERENCES meta.source_release,
  source_asset_id uuid,
  source_row_key text,
  raw_value jsonb,
  effective_date date,
  method_policy_id uuid REFERENCES meta.policy_version(policy_version_id),
  evidence jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(evidence) = 'object'),
  alternatives jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(alternatives) = 'array'),
  PRIMARY KEY (snapshot_id, building_id, variable_code),
  CHECK ((field_status IN ('present','conflicting') AND evidence_origin IS NOT NULL)
      OR (field_status IN ('missing','outside_scope','not_applicable') AND evidence_origin IS NULL)),
  CHECK (source_asset_id IS NULL OR source_release_id IS NOT NULL),
  FOREIGN KEY (snapshot_id, building_id) REFERENCES stock.building_snapshot,
  FOREIGN KEY (source_release_id, source_asset_id) REFERENCES meta.source_asset(source_release_id, source_asset_id)
);
COMMENT ON TABLE stock.field_lineage IS 'Field-specific provenance and alternatives; no universal confidence score. Absence of a lineage row is unassessed, not verified.';
CREATE TABLE meta.default_snapshot (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  snapshot_id uuid NOT NULL REFERENCES stock.snapshot
);

CREATE TABLE epc.certificate (
  certificate_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_release_id uuid NOT NULL REFERENCES meta.source_release,
  source_asset_id uuid,
  source_row_key text NOT NULL,
  formular_id text NOT NULL,
  form_version text NOT NULL,
  approval_date date,
  measurement_start date,
  measurement_end date,
  reported_use_code text,
  property_key text,
  area_scope_code text NOT NULL CHECK (area_scope_code IN ('single_building','multiple_buildings','property','unknown')),
  reported_atemp_m2 double precision CHECK (reported_atemp_m2 > 0 AND reported_atemp_m2 < 'Infinity'::float8),
  raw_payload jsonb NOT NULL CHECK (jsonb_typeof(raw_payload) = 'object'),
  CHECK (measurement_end IS NULL OR measurement_start IS NULL OR measurement_end >= measurement_start),
  UNIQUE (source_release_id, formular_id),
  FOREIGN KEY (source_release_id, source_asset_id) REFERENCES meta.source_asset(source_release_id, source_asset_id)
);
CREATE TABLE epc.certificate_building_link (
  certificate_id uuid NOT NULL REFERENCES epc.certificate,
  building_id uuid NOT NULL REFERENCES stock.building,
  source_release_id uuid NOT NULL REFERENCES meta.source_release,
  epc_subject_key text NOT NULL,
  decision text NOT NULL CHECK (decision IN ('accepted','rejected','unresolved')),
  reason_code text NOT NULL,
  evidence_tier text,
  semantic_compatibility text CHECK (semantic_compatibility IN ('compatible','incompatible','unknown')),
  linkage_policy_id uuid NOT NULL REFERENCES meta.policy_version(policy_version_id),
  allocation_policy_id uuid REFERENCES meta.policy_version(policy_version_id),
  evidence jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(evidence) = 'object'),
  PRIMARY KEY (certificate_id, building_id, source_release_id, epc_subject_key)
);
COMMENT ON TABLE epc.certificate_building_link IS 'Many-to-many evidence. Spatial acceptance does not allocate certificate Atemp or energy to individual footprints.';
CREATE TABLE epc.energy_observation (
  energy_observation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  certificate_id uuid NOT NULL REFERENCES epc.certificate,
  source_release_id uuid NOT NULL REFERENCES meta.source_release,
  source_field text NOT NULL,
  carrier_code text NOT NULL,
  end_use_code text NOT NULL,
  period_start date NOT NULL,
  period_end date NOT NULL CHECK (period_end >= period_start),
  energy_kwh double precision NOT NULL CHECK (energy_kwh >= 0 AND energy_kwh < 'Infinity'::float8),
  energy_basis text NOT NULL CHECK (energy_basis IN ('measured','calculated','unknown')),
  boundary_code text NOT NULL,
  correction_status text NOT NULL,
  scope_code text NOT NULL,
  epc_subject_key text,
  evidence jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(evidence) = 'object'),
  UNIQUE (certificate_id, source_release_id, source_field, period_start, period_end)
);

CREATE TABLE library.release (
  catalogue_release_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  catalogue_code text NOT NULL,
  version text NOT NULL,
  source_release_id uuid REFERENCES meta.source_release,
  status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published')),
  manifest jsonb NOT NULL CHECK (jsonb_typeof(manifest) = 'object'),
  manifest_sha256 text NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
  code_asset_id uuid REFERENCES meta.source_asset(source_asset_id),
  code_asset_sha256 text NOT NULL CHECK (code_asset_sha256 ~ '^[0-9a-f]{64}$'),
  UNIQUE (catalogue_code, version)
);
CREATE TABLE library.archetype (
  archetype_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  catalogue_release_id uuid NOT NULL REFERENCES library.release,
  category_code text NOT NULL,
  era_code text NOT NULL,
  climate_zone_code text NOT NULL,
  state_code text NOT NULL,
  ventilation_code text NOT NULL,
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
  UNIQUE (catalogue_release_id, category_code, era_code, climate_zone_code, state_code, ventilation_code)
);
CREATE TABLE model.scenario (
  scenario_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scenario_code text NOT NULL,
  version text NOT NULL,
  description text NOT NULL,
  status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published')),
  assumptions jsonb NOT NULL CHECK (jsonb_typeof(assumptions) = 'object'),
  UNIQUE (scenario_code, version)
);
CREATE TABLE model.measure (
  measure_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scenario_id uuid NOT NULL REFERENCES model.scenario,
  building_id uuid REFERENCES stock.building,
  sequence_no integer NOT NULL CHECK (sequence_no > 0),
  component_role text NOT NULL,
  effective_year integer NOT NULL CHECK (effective_year BETWEEN 1 AND 9999),
  evidence_origin text NOT NULL CHECK (evidence_origin IN ('reported','measured','derived','imputed','assumed')),
  source_asset_id uuid REFERENCES meta.source_asset(source_asset_id),
  policy_version_id uuid REFERENCES meta.policy_version(policy_version_id),
  spec_patch jsonb NOT NULL CHECK (jsonb_typeof(spec_patch) = 'object'),
  evidence jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(evidence) = 'object'),
  UNIQUE NULLS NOT DISTINCT (scenario_id, sequence_no, building_id)
);
COMMENT ON TABLE model.measure IS 'Scenario-only changes ordered by sequence_no within each target. NULL building_id means scenario-wide; precedence between global and building-specific changes requires an explicit policy.';
CREATE TABLE model.revision (
  model_revision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  snapshot_id uuid NOT NULL,
  building_id uuid NOT NULL,
  scenario_id uuid NOT NULL REFERENCES model.scenario,
  catalogue_release_id uuid NOT NULL REFERENCES library.release,
  selection_policy_id uuid NOT NULL REFERENCES meta.policy_version(policy_version_id),
  imputation_policy_id uuid REFERENCES meta.policy_version(policy_version_id),
  renovation_policy_id uuid REFERENCES meta.policy_version(policy_version_id),
  weather_asset_id uuid NOT NULL REFERENCES meta.source_asset(source_asset_id),
  compiler_name text NOT NULL,
  compiler_version text NOT NULL,
  compiler_code_sha256 text NOT NULL CHECK (compiler_code_sha256 ~ '^[0-9a-f]{64}$'),
  status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','finalized')),
  resolved_building_spec jsonb CHECK (resolved_building_spec IS NULL OR jsonb_typeof(resolved_building_spec) = 'object'),
  spec_sha256 text CHECK (spec_sha256 ~ '^[0-9a-f]{64}$'),
  physical_sha256 text CHECK (physical_sha256 ~ '^[0-9a-f]{64}$'),
  requirements jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(requirements) = 'array'),
  provenance jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(provenance) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (status <> 'finalized' OR (resolved_building_spec IS NOT NULL AND spec_sha256 IS NOT NULL
                                  AND physical_sha256 IS NOT NULL AND requirements = '[]'::jsonb)),
  FOREIGN KEY (snapshot_id, building_id) REFERENCES stock.building_snapshot
);
CREATE TABLE model.simulation_run (
  simulation_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  model_revision_id uuid NOT NULL REFERENCES model.revision,
  status text NOT NULL CHECK (status IN ('planned','running','completed','failed')),
  engine_name text NOT NULL,
  engine_version text NOT NULL,
  engine_binary_sha256 text CHECK (engine_binary_sha256 ~ '^[0-9a-f]{64}$'),
  actual_core_run_id text,
  started_at timestamptz,
  completed_at timestamptz,
  artifact_manifest jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(artifact_manifest) = 'array'),
  annual_scalars jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(annual_scalars) = 'object'),
  failure_detail text,
  CHECK (status <> 'completed' OR (engine_binary_sha256 IS NOT NULL AND nullif(btrim(actual_core_run_id),'') IS NOT NULL))
);
COMMENT ON COLUMN model.revision.spec_sha256 IS 'Assembler-computed identity of the complete resolved BuildingSpec, including accounting assumptions.';
COMMENT ON COLUMN model.revision.physical_sha256 IS 'Assembler-computed physical-input identity under an explicit versioned hashing contract; separate from full spec and actual execution identity.';
COMMENT ON COLUMN model.simulation_run.actual_core_run_id IS 'Actual EPSM Core execution/cache identity supplied by the runner, not invented from a database row UUID.';
COMMENT ON TABLE model.simulation_run IS 'Bulk EPW/epJSON/hourly outputs remain in external assets; store checksummed artifact URIs and small annual summaries.';

CREATE TABLE qa.scope_definition (
  scope_definition_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scope_code text NOT NULL,
  version text NOT NULL,
  counting_unit text NOT NULL CHECK (counting_unit IN ('building','certificate','subject','property','relation','area')),
  rule_policy_id uuid NOT NULL REFERENCES meta.policy_version(policy_version_id),
  definition jsonb NOT NULL CHECK (jsonb_typeof(definition) = 'object'),
  UNIQUE (scope_code, version)
);
CREATE TABLE qa.report (
  report_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  report_code text NOT NULL,
  version text NOT NULL,
  snapshot_id uuid REFERENCES stock.snapshot,
  source_release_id uuid REFERENCES meta.source_release,
  status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published')),
  method_policy_id uuid NOT NULL REFERENCES meta.policy_version(policy_version_id),
  manifest jsonb NOT NULL CHECK (jsonb_typeof(manifest) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (report_code, version)
);
CREATE TABLE qa.coverage_aggregate (
  report_id uuid NOT NULL REFERENCES qa.report,
  scope_definition_id uuid NOT NULL REFERENCES qa.scope_definition,
  metric_code text NOT NULL,
  use_code text,
  municipality_code text,
  variable_code text REFERENCES meta.variable,
  evidence_origin text CHECK (evidence_origin IN ('reported','measured','derived','imputed','assumed')),
  field_status text CHECK (field_status IN ('present','missing','conflicting','outside_scope','not_applicable')),
  numerator bigint NOT NULL CHECK (numerator >= 0),
  denominator bigint CHECK (denominator >= 0),
  unknown_denominator_reason text,
  CHECK (denominator IS NULL OR numerator <= denominator),
  CHECK (denominator IS NOT NULL OR nullif(btrim(unknown_denominator_reason), '') IS NOT NULL),
  UNIQUE NULLS NOT DISTINCT (report_id, scope_definition_id, metric_code, use_code, municipality_code, variable_code, evidence_origin, field_status)
);
COMMENT ON TABLE qa.coverage_aggregate IS 'Exact descriptive counts under a versioned scope rule. NULL denominator means unknown, never zero coverage; zero denominator also yields no percentage.';
CREATE TABLE qa.source_processing_scope (
  source_release_id uuid NOT NULL REFERENCES meta.source_release,
  area_release_id uuid NOT NULL,
  area_level text NOT NULL,
  area_code text NOT NULL,
  processing_status text NOT NULL CHECK (processing_status IN ('not_started','partial','complete','failed','outside_scope')),
  expected_count bigint CHECK (expected_count >= 0),
  included_count bigint CHECK (included_count >= 0),
  detail jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(detail) = 'object'),
  PRIMARY KEY (source_release_id, area_release_id, area_level, area_code),
  FOREIGN KEY (area_release_id, area_level, area_code) REFERENCES geo.area
);
CREATE TABLE qa.issue (
  issue_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_release_id uuid REFERENCES meta.source_release,
  snapshot_id uuid REFERENCES stock.snapshot,
  building_id uuid REFERENCES stock.building,
  variable_code text REFERENCES meta.variable,
  severity text NOT NULL CHECK (severity IN ('note','warning','error')),
  issue_code text NOT NULL,
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','acknowledged','resolved')),
  detail jsonb NOT NULL CHECK (jsonb_typeof(detail) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Guards protect OLD and NEW parents, preventing records from being moved out
-- of a frozen release. They enforce storage immutability, not scientific validity.
CREATE FUNCTION meta.guard_frozen_parent() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE old_parent uuid; new_parent uuid; frozen boolean;
BEGIN
  IF TG_OP <> 'INSERT' THEN old_parent := (to_jsonb(OLD)->>TG_ARGV[1])::uuid; END IF;
  IF TG_OP <> 'DELETE' THEN new_parent := (to_jsonb(NEW)->>TG_ARGV[1])::uuid; END IF;
  EXECUTE format('SELECT coalesce(bool_or(status = $3),false) FROM (SELECT status FROM %s WHERE %I IN ($1,$2) ORDER BY %I FOR SHARE) locked', TG_ARGV[0], TG_ARGV[1], TG_ARGV[1])
    INTO frozen USING old_parent, new_parent, TG_ARGV[2];
  IF frozen THEN RAISE EXCEPTION '% belongs to a frozen parent; create a new release/revision', TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME; END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;
CREATE FUNCTION meta.guard_frozen_self() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status = TG_ARGV[0] THEN RAISE EXCEPTION '% is frozen; create a new version', TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME; END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;
CREATE FUNCTION meta.guard_immutable_definition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION '% definitions are append-only; create a new code/version', TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME; END $$;
CREATE FUNCTION stock.check_publication() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE actual_count bigint;
BEGIN
  IF NEW.status = 'published' THEN
    SELECT count(*) INTO actual_count FROM stock.building_snapshot WHERE snapshot_id = NEW.snapshot_id;
    IF NEW.expected_building_count IS NOT NULL AND actual_count <> NEW.expected_building_count THEN
      RAISE EXCEPTION 'Snapshot count % differs from expected %', actual_count, NEW.expected_building_count;
    END IF;
    IF EXISTS (SELECT 1 FROM stock.snapshot_source s JOIN meta.source_release r USING (source_release_id)
               WHERE s.snapshot_id = NEW.snapshot_id AND r.status <> 'published') THEN
      RAISE EXCEPTION 'Publish pinned source releases before publishing a snapshot';
    END IF;
  END IF;
  RETURN NEW;
END $$;
COMMENT ON FUNCTION stock.check_publication() IS 'Minimal manifest/count check only; does not establish full national coverage, geometry validity, complete lineage or physical model readiness.';
CREATE FUNCTION meta.check_default_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM stock.snapshot WHERE snapshot_id = NEW.snapshot_id AND status = 'published') THEN
    RAISE EXCEPTION 'Default snapshot must be published';
  END IF;
  RETURN NEW;
END $$;
CREATE FUNCTION geo.check_context_definition() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE definition meta.variable%ROWTYPE;
BEGIN
  SELECT * INTO definition FROM meta.variable WHERE variable_code = NEW.variable_code;
  IF definition.requires_price_year AND NEW.price_year IS NULL THEN
    RAISE EXCEPTION 'Variable % requires an explicit price year', NEW.variable_code;
  END IF;
  IF (definition.value_type IN ('number','integer') AND NEW.value_numeric IS NULL)
     OR (definition.value_type = 'text' AND NEW.value_text IS NULL)
     OR (definition.value_type = 'boolean' AND NEW.value_boolean IS NULL)
     OR definition.value_type NOT IN ('number','integer','text','boolean') THEN
    RAISE EXCEPTION 'Context value type disagrees with variable %', NEW.variable_code;
  END IF;
  IF definition.value_type = 'integer' AND NEW.value_numeric <> trunc(NEW.value_numeric) THEN
    RAISE EXCEPTION 'Variable % requires an integer value', NEW.variable_code;
  END IF;
  RETURN NEW;
END $$;
CREATE FUNCTION model.check_finalization() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status = 'finalized' AND (
      NOT EXISTS (SELECT 1 FROM stock.snapshot WHERE snapshot_id = NEW.snapshot_id AND status = 'published')
      OR NOT EXISTS (SELECT 1 FROM library.release WHERE catalogue_release_id = NEW.catalogue_release_id AND status = 'published')
      OR NOT EXISTS (SELECT 1 FROM model.scenario WHERE scenario_id = NEW.scenario_id AND status = 'published')
      OR NOT EXISTS (SELECT 1 FROM meta.source_asset a JOIN meta.source_release r USING (source_release_id)
                     WHERE a.source_asset_id = NEW.weather_asset_id AND r.status = 'published')) THEN
    RAISE EXCEPTION 'Finalized revisions require published snapshot, catalogue, scenario and weather release';
  END IF;
  RETURN NEW;
END $$;
COMMENT ON FUNCTION model.check_finalization() IS 'Pins frozen inputs; BuildingSpec validation, checksum verification, weather compatibility and physical readiness remain assembler responsibilities.';

DO $$
DECLARE item text[];
BEGIN
  FOREACH item SLICE 1 IN ARRAY ARRAY[
    ['meta.source_asset','meta.source_release','source_release_id','published'],
    ['meta.source_variable','meta.source_release','source_release_id','published'],
    ['geo.area','meta.source_release','source_release_id','published'],
    ['geo.context_observation','meta.source_release','source_release_id','published'],
    ['stock.footprint','meta.source_release','source_release_id','published'],
    ['epc.certificate','meta.source_release','source_release_id','published'],
    ['epc.certificate_building_link','meta.source_release','source_release_id','published'],
    ['epc.energy_observation','meta.source_release','source_release_id','published'],
    ['qa.source_processing_scope','meta.source_release','source_release_id','published'],
    ['stock.snapshot_source','stock.snapshot','snapshot_id','published'],
    ['stock.building_snapshot','stock.snapshot','snapshot_id','published'],
    ['stock.building_area_assignment','stock.snapshot','snapshot_id','published'],
    ['stock.field_lineage','stock.snapshot','snapshot_id','published'],
    ['library.archetype','library.release','catalogue_release_id','published'],
    ['model.measure','model.scenario','scenario_id','published'],
    ['qa.coverage_aggregate','qa.report','report_id','published']
  ] LOOP
    EXECUTE format('CREATE TRIGGER freeze_parent BEFORE INSERT OR UPDATE OR DELETE ON %s FOR EACH ROW EXECUTE FUNCTION meta.guard_frozen_parent(%L,%L,%L)', item[1],item[2],item[3],item[4]);
  END LOOP;
  FOREACH item SLICE 1 IN ARRAY ARRAY[
    ['meta.source_release','published'],['stock.snapshot','published'],
    ['library.release','published'],['model.scenario','published'],
    ['model.revision','finalized'],['qa.report','published']
  ] LOOP
    EXECUTE format('CREATE TRIGGER freeze_self BEFORE UPDATE OR DELETE ON %s FOR EACH ROW EXECUTE FUNCTION meta.guard_frozen_self(%L)',item[1],item[2]);
  END LOOP;
  FOREACH item SLICE 1 IN ARRAY ARRAY[['meta.policy_version'],['meta.variable'],['qa.scope_definition'],['stock.building']] LOOP
    EXECUTE format('CREATE TRIGGER freeze_definition BEFORE UPDATE OR DELETE ON %s FOR EACH ROW EXECUTE FUNCTION meta.guard_immutable_definition()',item[1]);
  END LOOP;
END $$;
CREATE TRIGGER check_publication BEFORE INSERT OR UPDATE ON stock.snapshot FOR EACH ROW EXECUTE FUNCTION stock.check_publication();
CREATE TRIGGER check_default_snapshot BEFORE INSERT OR UPDATE ON meta.default_snapshot FOR EACH ROW EXECUTE FUNCTION meta.check_default_snapshot();
CREATE TRIGGER check_context_definition BEFORE INSERT OR UPDATE ON geo.context_observation FOR EACH ROW EXECUTE FUNCTION geo.check_context_definition();
CREATE TRIGGER check_finalization BEFORE INSERT OR UPDATE ON model.revision FOR EACH ROW EXECUTE FUNCTION model.check_finalization();

CREATE INDEX building_snapshot_use_idx ON stock.building_snapshot(snapshot_id, primary_use_code, municipality_code);
CREATE INDEX building_snapshot_epc_use_idx ON stock.building_snapshot(snapshot_id, epc_use_code);
CREATE INDEX building_snapshot_year_idx ON stock.building_snapshot(snapshot_id, construction_year);
CREATE INDEX building_snapshot_building_idx ON stock.building_snapshot(building_id, snapshot_id);
CREATE INDEX footprint_building_idx ON stock.footprint(building_id, source_release_id);
CREATE INDEX field_lineage_variable_idx ON stock.field_lineage(snapshot_id, variable_code, field_status, evidence_origin);
CREATE INDEX area_assignment_area_idx ON stock.building_area_assignment(area_release_id, area_level, area_code, snapshot_id);
CREATE INDEX context_area_variable_year_idx ON geo.context_observation(area_release_id, area_level, area_code, variable_code, reference_year);
CREATE INDEX epc_link_building_idx ON epc.certificate_building_link(building_id, decision);
CREATE INDEX epc_energy_period_idx ON epc.energy_observation(period_start, period_end, carrier_code, end_use_code);
CREATE INDEX model_revision_building_idx ON model.revision(snapshot_id, building_id, scenario_id);
CREATE INDEX simulation_revision_idx ON model.simulation_run(model_revision_id, status);
CREATE INDEX qa_issue_building_idx ON qa.issue(snapshot_id, building_id, severity);

CREATE VIEW api.buildings_all_snapshots AS
SELECT b.*, f.geometry_sha256 AS footprint_geometry_sha256,
       f.source_row_key AS footprint_row_key, f.outline_kind, f.geometry_status AS footprint_geometry_status
FROM stock.building_snapshot b
JOIN stock.footprint f ON f.source_release_id = b.footprint_release_id AND f.building_id = b.building_id;
CREATE VIEW api.buildings AS
SELECT b.* FROM api.buildings_all_snapshots b JOIN meta.default_snapshot d USING (snapshot_id);
CREATE VIEW api.variable_dictionary AS SELECT * FROM meta.variable;
CREATE VIEW api.coverage AS
SELECT c.*, c.numerator::numeric / nullif(c.denominator,0) AS fraction,
       s.counting_unit, s.scope_code, s.version AS scope_version
FROM qa.coverage_aggregate c JOIN qa.scope_definition s USING (scope_definition_id)
JOIN qa.report r USING (report_id) WHERE r.status = 'published';
COMMENT ON VIEW api.buildings IS 'One published default snapshot; all physical buildings survive sparse evidence. Empty until a default is explicitly selected.';

INSERT INTO meta.variable(variable_code,label,definition,value_type,unit_code,reference_basis,requires_price_year,dictionary_version) VALUES
('construction_year','Construction year','Selected construction year; source alternatives and precedence are retained in lineage.','integer','year','physical building',false,'1'),
('primary_use_code','Primary GIS use','Primary physical-stock purpose; unknown values remain in stock.','text','category','physical building',false,'1'),
('epc_use_code','Reported EPC programme','Selected reported heated use; separate from GIS purpose.','text','category','EPC subject with reviewed building scope',false,'1'),
('height_representative_m','Representative height','LiDAR-derived representative height above local ground, as defined by the pinned release.','number','m','physical building and capture date',false,'1'),
('height_eave_m','Eave height','Eave height above local ground; not ridge height or storey clear height.','number','m','physical building and capture date',false,'1'),
('height_ridge_m','Ridge height','Ridge height above local ground.','number','m','physical building and capture date',false,'1'),
('roof_form_code','Roof form','Selected roof-form classification; extraction support is separate from height availability.','text','category','physical building',false,'1'),
('storeys_above_ground','Above-ground storeys','Above-ground storey count, excluding basement storeys.','integer','count','physical building',false,'1'),
('heated_basement_storeys','Heated basement storeys','Reported heated basement count; zero does not establish absence of an unheated basement.','integer','count','heated building extent',false,'1'),
('heated_floor_area_atemp_m2','Atemp','Building-scoped heated floor area for loads, independent of geometric footprint/floor area.','number','m2','building-scoped heated extent',false,'1'),
('footprint_area_m2','Footprint area','Metric horizontal area of the selected footprint geometry.','number','m2','selected physical footprint',false,'1'),
('ventilation_system_code','Ventilation topology','Reported/selected system categories; multiple and unknown systems need explicit handling.','text','category','building and effective date',false,'1'),
('ventilation_design_flow_l_s_m2','Design ventilation flow','Reported design flow; not measured flow or infiltration.','number','L/s/m2','declared reference floor area',false,'1'),
('heating_systems','Heating systems','Selected heating-system evidence; carrier energy does not establish capacity or efficiency.','json','category','building and effective date',false,'1'),
('population_density_people_km2','Population density','Population per square kilometre under the source spatial support and reference year.','number','people/km2','versioned area or grid cell',false,'1'),
('population_total','Population count','Number of people within the source-defined spatial support and reference year.','integer','people','versioned area or grid cell',false,'1'),
('heating_degree_days_k_day','Heating degree days','Degree-day statistic; base temperature, weather year and correction method must be retained.','number','K.day','versioned area and declared base temperature',false,'1');
COMMIT;
