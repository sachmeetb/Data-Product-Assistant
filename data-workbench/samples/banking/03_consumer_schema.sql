-- =============================================================================
-- Data Workbench sample dataset — Banking EDW
-- File 3 of 3: consumer-view schema
--
-- Target schema for Workbench-generated consumer views (data products built
-- ON TOP of the edw source schema). Kept separate from 01_schema.sql so that
-- reloading the source data does NOT drop deployed product views.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS banking_views;

COMMENT ON SCHEMA banking_views IS 'Workbench-managed consumer views over the edw banking source schema.';
