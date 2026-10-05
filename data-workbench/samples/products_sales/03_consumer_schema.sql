-- =============================================================================
-- Data Workbench sample dataset — Products & Sales
-- File 3 of 3: consumer schema (target for served views)
--
-- The Workbench's `data-serving-virtual-view` skill generates CREATE VIEW
-- DDL but does NOT yet execute it on Postgres. This file pre-creates the
-- `wb_views` schema so the engineer can manually load the generated DDL
-- with `psql -f projects/<code>/serving/virtual_view.sql` once it lands.
--
-- See samples/products_sales/README.md for the full workflow.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS wb_views;

COMMENT ON SCHEMA wb_views IS 'Target schema for Data Workbench-generated views over products_sales source tables.';
