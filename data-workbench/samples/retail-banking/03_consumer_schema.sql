-- =============================================================================
-- Data Workbench sample dataset — Retail Banking (brownfield estate)
-- File 3 of 3: consumer-view schema
--
-- Target schema for Workbench-generated consumer/serving views (data products
-- built ON TOP of the source schemas). Kept separate from 01_schema.sql so that
-- reloading the source data does NOT drop deployed product views.
--
-- Exclude this schema from the Connected-Estate scan so the estate presents a
-- clean "sources only" inventory:
--   source namespace_policy → {"mode":"exclude","namespaces":["retailbank_views"]}
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS retailbank_views;

COMMENT ON SCHEMA retailbank_views IS 'Workbench-managed consumer/serving views over the retail-banking source schemas.';
