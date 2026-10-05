#!/usr/bin/env python3
"""
Summarize a column_context JSON file into a compact, human-readable format.

Used to condense large context files (many tables/columns) into a form that
fits within a language model's context window for description generation.

For each column, prints:
  - table.column, data type, PK/nullable flags
  - FK references (if any)
  - Rule types and descriptions
  - Allowed values for low-cardinality columns (distinct_count <= 20)
  - Sibling column names (for table context)

Usage:
    python summarize_context.py <context_file>

    context_file   path to column_context_<timestamp>.json
"""

import sys
import json
import argparse


LOW_CARDINALITY_THRESHOLD = 20


def summarize(context_file: str) -> None:
    with open(context_file) as f:
        data = json.load(f)

    current_table = None

    for col in data:
        table = f"{col['schema']}.{col['table_name']}"

        if table != current_table:
            current_table = table
            siblings = [c['name'] for c in col.get('all_table_columns', [])]
            fk_summary = []
            for fk in col.get('foreign_keys', []):
                if fk.get('columns'):
                    fk_summary.append(
                        f"{fk['columns']} -> "
                        f"{fk.get('referencedSchema')}.{fk.get('referencedTable')}"
                        f"({fk.get('referencedColumns')})"
                    )
            print(f"\n{'='*60}")
            print(f"TABLE: {table}  (row_count={col.get('row_count')})")
            print(f"  columns: {', '.join(siblings)}")
            if fk_summary:
                print(f"  foreign_keys: {'; '.join(fk_summary)}")
            print()

        flags = []
        if col.get('primary_key'):
            flags.append('PK')
        if not col.get('nullable'):
            flags.append('NOT NULL')
        flag_str = '  [' + ', '.join(flags) + ']' if flags else ''

        print(f"  {col['column_name']}  ({col['data_type']}){flag_str}")

        # FK: check if this column is part of any FK
        col_name = col['column_name']
        for fk in col.get('foreign_keys', []):
            fk_cols = [c.strip() for c in (fk.get('columns') or '').split(',')]
            if col_name in fk_cols:
                idx = fk_cols.index(col_name)
                ref_cols = [c.strip() for c in (fk.get('referencedColumns') or '').split(',')]
                ref_col = ref_cols[idx] if idx < len(ref_cols) else '?'
                print(
                    f"    FK -> {fk.get('referencedSchema')}.{fk.get('referencedTable')}.{ref_col}"
                )

        # Rules
        for rule in col.get('rules', []):
            print(f"    rule[{rule['ruleType']}]: {rule['description']}")

        # Low-cardinality top values
        measurements = col.get('measurements', [])
        distinct_count = next(
            (m['value'] for m in measurements if m['metric'] == 'distinct_count'), None
        )
        top_values = col.get('top_values', [])
        if top_values and distinct_count is not None and distinct_count <= LOW_CARDINALITY_THRESHOLD:
            vals = [str(tv['value']) for tv in top_values]
            print(f"    allowed_values: {vals}")


def main():
    parser = argparse.ArgumentParser(
        description="Summarize a column_context JSON file for description generation.",
    )
    parser.add_argument("context_file", help="Path to column_context_<timestamp>.json")
    args = parser.parse_args()
    summarize(args.context_file)


if __name__ == "__main__":
    main()
