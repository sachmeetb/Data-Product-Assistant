"""
databricks_hydrator.py — turn catalog TableSpecs into Unity Catalog schemas + Delta tables.
"""

from dataclasses import dataclass
from type_mapping import to_databricks_type

_VALID_LAYERS = {"bronze", "silver", "gold"}


@dataclass
class HydrationPlan:
    """The full set of statements to bring Databricks Unity Catalog up to the catalog."""
    schemas: list
    tables: list
    inserts: list
    skipped: list = None

    def __post_init__(self):
        if self.skipped is None:
            self.skipped = []


def build_hydration_plan(tables: list, catalog: str = "main", replace: bool = False) -> HydrationPlan:
    schemas = []
    seen_schemas = set()
    ddl_tables = []
    skipped = []

    for tbl in tables:
        layer = (getattr(tbl, "layer", None) or "gold").lower()
        if layer not in _VALID_LAYERS:
            layer = "gold"
        if layer not in seen_schemas:
            seen_schemas.add(layer)
            schemas.append(f"CREATE SCHEMA IF NOT EXISTS `{catalog}`.`{layer}`;")

        cols = getattr(tbl, "columns", []) or []
        if not cols:
            skipped.append(tbl.name)
            continue

        col_lines = []
        for col in cols:
            dbx_type = to_databricks_type(getattr(col, "data_type", "STRING"))
            null_clause = "" if getattr(col, "nullable", True) else " NOT NULL"
            comment = (getattr(col, "description", "") or "").replace("'", "''")
            comment_clause = f" COMMENT '{comment}'" if comment else ""
            col_lines.append(f"  `{col.name}` {dbx_type}{null_clause}{comment_clause}")

        create_kw = "CREATE OR REPLACE TABLE" if replace else "CREATE TABLE IF NOT EXISTS"
        ddl = (
            f"{create_kw} `{catalog}`.`{layer}`.`{tbl.name}` (\n"
            + ",\n".join(col_lines)
            + "\n) USING DELTA;"
        )
        ddl_tables.append(ddl)

    return HydrationPlan(schemas=schemas, tables=ddl_tables, inserts=[], skipped=skipped)


def execute_plan(plan: HydrationPlan, catalog: str = "main", mode: str = "auto") -> dict:
    from tools.databricks_tool import DatabricksPublisher

    sql = "\n".join(plan.schemas + plan.tables + plan.inserts)
    pub = DatabricksPublisher(catalog=catalog, mode=mode)
    return pub.publish(sql)
