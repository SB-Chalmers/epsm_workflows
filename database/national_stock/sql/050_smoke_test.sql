-- Meaningful relational checks against synthetic data. Always rolls back.
-- Run after 010 and 030; 020 is optional for this relational test.
BEGIN;
DO $$
DECLARE
  src uuid := gen_random_uuid(); rel uuid := gen_random_uuid();
  asset uuid := gen_random_uuid(); policy uuid := gen_random_uuid();
  snap uuid := gen_random_uuid(); snap_bad uuid := gen_random_uuid();
  b1 uuid := gen_random_uuid(); b2 uuid := gen_random_uuid(); b3 uuid := gen_random_uuid();
  cert uuid := gen_random_uuid(); area text := 'test_grid';
  scope uuid := gen_random_uuid(); test_report_id uuid := gen_random_uuid();
  cat uuid := gen_random_uuid(); test_scenario_id uuid := gen_random_uuid(); test_revision_id uuid := gen_random_uuid();
  expected_failure boolean;
BEGIN
  INSERT INTO meta.source(source_id,source_code,publisher,description)
    VALUES(src,'synthetic-test-'||src,'test','Synthetic relational smoke test');
  INSERT INTO meta.source_release(source_release_id,source_id,release_tag,manifest)
    VALUES(rel,src,'test','{}');
  INSERT INTO meta.source_asset(source_asset_id,source_release_id,asset_role,uri,content_sha256)
    VALUES(asset,rel,'test','test://asset',repeat('a',64));
  INSERT INTO meta.policy_version(policy_version_id,policy_code,version,definition,content_sha256)
    VALUES(policy,'test-'||policy,'1','{}',repeat('b',64));
  INSERT INTO stock.building(building_id,identity_namespace) VALUES(b1,'test'),(b2,'test'),(b3,'test');
  INSERT INTO stock.footprint(source_release_id,building_id,source_asset_id,source_row_key,geometry_sha256,geometry_status,source_crs)
    VALUES(rel,b1,asset,'1',repeat('c',64),'unreviewed','EPSG:3006'),
          (rel,b2,asset,'2',repeat('d',64),'unreviewed','EPSG:3006'),
          (rel,b3,asset,'3',repeat('e',64),'unreviewed','EPSG:3006');
  INSERT INTO stock.snapshot(snapshot_id,snapshot_code,version,as_of_date,expected_building_count,selection_policy_id,manifest)
    VALUES(snap,'test-'||snap,'1',DATE '2026-10-05',3,policy,'{}');
  INSERT INTO stock.snapshot_source VALUES(snap,rel,'test');
  INSERT INTO stock.building_snapshot(snapshot_id,building_id,footprint_release_id,municipality_code,primary_use_code,
                                       construction_year,height_representative_m,storeys_above_ground,heated_floor_area_atemp_m2)
    VALUES(snap,b1,rel,'1480','multi_family',1964,12,4,1000),
          (snap,b2,rel,'1480','single_family',NULL,6,2,NULL),
          (snap,b3,rel,'1480','school',NULL,NULL,NULL,NULL);
  INSERT INTO stock.field_lineage(snapshot_id,building_id,variable_code,field_status,evidence_origin,source_release_id,source_asset_id,source_row_key)
    VALUES(snap,b1,'construction_year','conflicting','reported',rel,asset,'1');
  INSERT INTO stock.field_lineage(snapshot_id,building_id,variable_code,field_status)
    VALUES(snap,b2,'construction_year','missing'),(snap,b3,'construction_year','missing');
  INSERT INTO epc.certificate(certificate_id,source_release_id,source_asset_id,source_row_key,formular_id,form_version,area_scope_code,reported_atemp_m2,raw_payload)
    VALUES(cert,rel,asset,'epc1','test-epc','test','multiple_buildings',1600,'{}');
  INSERT INTO epc.certificate_building_link(certificate_id,building_id,source_release_id,epc_subject_key,decision,reason_code,linkage_policy_id)
    VALUES(cert,b1,rel,'subject1','accepted','test',policy),(cert,b2,rel,'subject2','accepted','test',policy);
  IF (SELECT count(*) FROM stock.building_snapshot WHERE snapshot_id=snap) <> 3 THEN RAISE EXCEPTION 'Sparse evidence shrank stock'; END IF;
  IF (SELECT count(*) FROM stock.building_snapshot WHERE snapshot_id=snap AND heated_floor_area_atemp_m2 IS NULL) <> 2 THEN
    RAISE EXCEPTION 'Certificate area was broadcast across footprints';
  END IF;
  INSERT INTO geo.area(source_release_id,area_level,area_code,name) VALUES(rel,'population_grid_1km',area,'test');
  INSERT INTO geo.context_observation(source_release_id,source_asset_id,area_release_id,area_level,area_code,variable_code,
                                      reference_year,unit_code,reference_basis,value_numeric,evidence_origin)
    VALUES(rel,asset,rel,'population_grid_1km',area,'population_total',2025,'people','1 km cell',20,'reported');
  expected_failure := false;
  BEGIN
    INSERT INTO geo.context_observation(source_release_id,area_release_id,area_level,area_code,variable_code,
                                        reference_year,unit_code,reference_basis,value_numeric,evidence_origin)
      VALUES(rel,rel,'population_grid_1km',area,'population_total',2025,'people','1 km cell',20.5,'reported');
  EXCEPTION WHEN raise_exception THEN expected_failure := true; END;
  IF NOT expected_failure THEN RAISE EXCEPTION 'Fractional population integer accepted'; END IF;

  INSERT INTO stock.snapshot(snapshot_id,snapshot_code,version,as_of_date,expected_building_count,selection_policy_id,manifest)
    VALUES(snap_bad,'test-bad-'||snap_bad,'1',DATE '2026-10-05',5,policy,'{}');
  expected_failure := false;
  BEGIN UPDATE stock.snapshot SET status='published' WHERE snapshot_id=snap_bad;
  EXCEPTION WHEN raise_exception THEN expected_failure := true; END;
  IF NOT expected_failure THEN RAISE EXCEPTION 'Wrong snapshot count published'; END IF;
  UPDATE meta.source_release SET status='published' WHERE source_release_id=rel;
  UPDATE stock.snapshot SET status='published' WHERE snapshot_id=snap;
  INSERT INTO meta.default_snapshot(singleton,snapshot_id) VALUES(true,snap)
    ON CONFLICT(singleton) DO UPDATE SET snapshot_id=excluded.snapshot_id;
  IF (SELECT count(*) FROM api.buildings) <> 3 THEN RAISE EXCEPTION 'Default API population changed'; END IF;
  IF (SELECT count(*) FROM api.building_evidence WHERE snapshot_id=snap) <> 3 THEN RAISE EXCEPTION 'Missing/conflicting evidence lost'; END IF;
  expected_failure := false;
  BEGIN UPDATE stock.building_snapshot SET construction_year=2000 WHERE snapshot_id=snap AND building_id=b1;
  EXCEPTION WHEN raise_exception THEN expected_failure := true; END;
  IF NOT expected_failure THEN RAISE EXCEPTION 'Published stock mutated'; END IF;
  expected_failure := false;
  BEGIN UPDATE stock.footprint SET source_row_key='changed' WHERE source_release_id=rel AND building_id=b1;
  EXCEPTION WHEN raise_exception THEN expected_failure := true; END;
  IF NOT expected_failure THEN RAISE EXCEPTION 'Published source geometry mutated'; END IF;

  INSERT INTO qa.scope_definition(scope_definition_id,scope_code,version,counting_unit,rule_policy_id,definition)
    VALUES(scope,'test-stock-'||scope,'1','building',policy,'{}');
  INSERT INTO qa.report(report_id,report_code,version,snapshot_id,method_policy_id,manifest)
    VALUES(test_report_id,'test-report-'||test_report_id,'1',snap,policy,'{}');
  INSERT INTO qa.coverage_aggregate(report_id,scope_definition_id,metric_code,use_code,numerator,denominator,unknown_denominator_reason)
    VALUES(test_report_id,scope,'year_present','all',1,3,NULL),
          (test_report_id,scope,'office_population','office',0,NULL,'National office denominator not established');
  UPDATE qa.report SET status='published' WHERE report_id=test_report_id;
  IF EXISTS(SELECT FROM api.coverage WHERE report_id=test_report_id AND use_code='office' AND fraction IS NOT NULL) THEN
    RAISE EXCEPTION 'Unknown denominator became zero coverage';
  END IF;
  expected_failure := false;
  BEGIN UPDATE qa.coverage_aggregate SET numerator=2 WHERE report_id=test_report_id AND use_code='all';
  EXCEPTION WHEN raise_exception THEN expected_failure := true; END;
  IF NOT expected_failure THEN RAISE EXCEPTION 'Published coverage mutated'; END IF;

  INSERT INTO library.release(catalogue_release_id,catalogue_code,version,manifest,manifest_sha256,code_asset_sha256)
    VALUES(cat,'test-'||cat,'1','{}',repeat('a',64),repeat('b',64));
  UPDATE library.release SET status='published' WHERE catalogue_release_id=cat;
  INSERT INTO model.scenario(scenario_id,scenario_code,version,description,assumptions)
    VALUES(test_scenario_id,'test-'||test_scenario_id,'1','test','{}');
  UPDATE model.scenario SET status='published' WHERE scenario_id=test_scenario_id;
  INSERT INTO model.revision(model_revision_id,snapshot_id,building_id,scenario_id,catalogue_release_id,selection_policy_id,
                             weather_asset_id,compiler_name,compiler_version,compiler_code_sha256,
                             resolved_building_spec,spec_sha256,physical_sha256,requirements)
    VALUES(test_revision_id,snap,b1,test_scenario_id,cat,policy,asset,'test','1',repeat('c',64),'{}',repeat('d',64),repeat('e',64),'["unresolved"]');
  expected_failure := false;
  BEGIN UPDATE model.revision SET status='finalized' WHERE model_revision_id=test_revision_id;
  EXCEPTION WHEN check_violation THEN expected_failure := true; END;
  IF NOT expected_failure THEN RAISE EXCEPTION 'Unresolved model finalized'; END IF;
  UPDATE model.revision SET requirements='[]',status='finalized' WHERE model_revision_id=test_revision_id;
  expected_failure := false;
  BEGIN UPDATE model.revision SET resolved_building_spec='{"changed":true}' WHERE model_revision_id=test_revision_id;
  EXCEPTION WHEN raise_exception THEN expected_failure := true; END;
  IF NOT expected_failure THEN RAISE EXCEPTION 'Finalized model mutated'; END IF;
  IF has_table_privilege('epsm_worker','stock.building_snapshot','UPDATE') THEN RAISE EXCEPTION 'Worker can rewrite stock'; END IF;
  IF NOT has_table_privilege('epsm_reader','api.buildings','SELECT') THEN RAISE EXCEPTION 'Reader cannot query API'; END IF;
  RAISE NOTICE 'PASS: stock retention, EPC scope, provenance, integer context, publication, immutability, unknown denominators, model finalization and roles';
END $$;
ROLLBACK;
