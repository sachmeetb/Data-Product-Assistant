-- ============================================================================
-- HR demo dataset — consumer view target schema
-- ============================================================================
-- The Workbench's serving stage writes the deployed consumer-aligned views
-- (vw_<dataset>) into this schema. Both source products' views co-exist here so
-- a consumer product can join across them on a single Postgres instance.
--
-- Kept separate from 01_schema.sql so re-loading the source (which drops
-- hr_core / hr_comp) does not wipe deployed views. Run once after 01/02.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS hr_views;
COMMENT ON SCHEMA hr_views IS 'Target schema for Data Workbench consumer-aligned views over the HR source products.';
