-- ============================================================================
-- HR demo dataset — consumer view target schema (MySQL 8.0+)
-- ============================================================================
-- MySQL port of samples/hr/03_consumer_schema.sql.
-- Do NOT modify the Postgres originals.
--
-- The Workbench's serving stage writes the deployed consumer-aligned views
-- (vw_<dataset>) into this schema.  Both source products' views co-exist here so
-- a consumer product can join across them on a single MySQL instance.
--
-- Kept separate from 01_schema.sql so re-loading the source (which drops
-- hr_core / hr_comp) does not wipe deployed views.  Run once after 01/02.
--
-- Note: MySQL does not support COMMENT ON SCHEMA as a separate statement.
-- The schema comment is embedded in the CREATE SCHEMA clause below.
-- ============================================================================

-- Note: MySQL does not support COMMENT on CREATE SCHEMA; description omitted.
CREATE SCHEMA IF NOT EXISTS hr_views
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_unicode_ci;
