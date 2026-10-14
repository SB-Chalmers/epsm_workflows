-- Apply after 010_foundation.sql, on a server with PostGIS installed.
BEGIN;
CREATE EXTENSION IF NOT EXISTS postgis;

-- Preserve the selected source geometry; invalid or unexpected types remain
-- explicit source evidence. Modelling adapters must validate polygon support.
ALTER TABLE stock.footprint ADD COLUMN footprint_geometry geometry(Geometry,3006);
ALTER TABLE geo.area ADD COLUMN area_geometry geometry(Geometry,3006);
CREATE INDEX footprint_geometry_gist ON stock.footprint USING gist(footprint_geometry);
CREATE INDEX area_geometry_gist ON geo.area USING gist(area_geometry);
COMMENT ON COLUMN stock.footprint.footprint_geometry IS
  'Selected source footprint in EPSG:3006. Preserve source geometry; repairs and modelling simplifications need separate provenance.';
COMMENT ON COLUMN geo.area.area_geometry IS
  'Versioned county, municipality, statistical-area or grid geometry in EPSG:3006; observation years are separate from boundary editions.';

CREATE VIEW api.buildings_spatial_all_snapshots AS
SELECT b.*, f.footprint_geometry
FROM api.buildings_all_snapshots b
JOIN stock.footprint f ON (f.source_release_id, f.building_id) = (b.footprint_release_id, b.building_id);
CREATE VIEW api.buildings_spatial AS
SELECT b.* FROM api.buildings_spatial_all_snapshots b
JOIN meta.default_snapshot d USING (snapshot_id);
CREATE VIEW api.areas AS
SELECT a.* FROM geo.area a JOIN meta.source_release r USING (source_release_id)
WHERE r.status = 'published';
COMMENT ON VIEW api.buildings_spatial IS
  'Default published stock including selected source geometry. A non-null geometry does not establish model-ready polygon topology.';
COMMIT;
