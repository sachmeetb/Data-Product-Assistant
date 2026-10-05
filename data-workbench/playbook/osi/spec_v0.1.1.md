<!--
Vendored from https://github.com/open-semantic-interchange/OSI/blob/main/core-spec/spec.md
License: Apache-2.0 (LICENSE) and CC-BY (LICENSE-Docs) per upstream repo.
Local copy used for offline grounding by the data-product-osi-advisor skill
and the deterministic validator in workbench/backend/osi.py.
-->

# OSI - Core Metadata Specification

**Version:** 0.1.1

## Goals

- **Standardization**: Establish uniform language and structure for semantic model definitions, ensuring consistency and ease of interpretation across various tools and systems.
- **Extensibility**: Support domain-specific extensions while maintaining core compatibility.
- **Interoperability**: Enable exchange and reuse across different AI and BI applications.

## Enumerations

### Dialects

Supported SQL and expression language dialects for metrics and field definitions.

| Dialect | Description |
|---------|-------------|
| `ANSI_SQL` | Standard SQL dialect |
| `SNOWFLAKE` | Snowflake SQL |
| `MDX` | Multi-Dimensional Expressions |
| `TABLEAU` | Tableau calculations |
| `DATABRICKS` | Databricks SQL |

### Vendors

Supported vendors for custom extensions and integrations.

| Vendor | Description |
|--------|-------------|
| `COMMON` | Common/standard extensions |
| `SNOWFLAKE` | Snowflake-specific attributes |
| `SALESFORCE` | Salesforce/Tableau-specific attributes |
| `DBT` | dbt-specific attributes |
| `DATABRICKS` | Databricks-specific attributes |

## Semantic Model

The top-level container that represents a complete semantic model, including datasets, relationships, and metrics.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Unique identifier for the semantic model |
| `description` | string | No | Human-readable description |
| `ai_context` | string/object | No | Additional context for AI tools (e.g., custom instructions) |
| `datasets` | array | Yes | Collection of logical datasets (fact and dimension tables) |
| `relationships` | array | No | Defines how logical datasets are connected |
| `metrics` | array | No | Quantifiable measures defined as aggregate expressions on fields from logical datasets |
| `custom_extensions` | array | No | Vendor-specific attributes for extensibility |

## Datasets

Logical datasets represent business entities or concepts (fact and dimension tables). They contain fields and define the structure of the data.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Unique identifier for the dataset |
| `source` | string | Yes | Reference to underlying physical table/view (e.g., `database.schema.table`) or query |
| `primary_key` | array | No | Primary key columns that uniquely identify rows (single or composite) |
| `unique_keys` | array of arrays | No | Array of unique key definitions (each can be single or composite) |
| `description` | string | No | Human-readable description |
| `ai_context` | string/object | No | Additional context for AI tools (e.g., synonyms, common terms) |
| `fields` | array | No | Row-level attributes for grouping, filtering, and metric expressions |
| `custom_extensions` | array | No | Vendor-specific attributes |

## Relationships

Relationships define how logical datasets are connected through foreign key constraints. They support both simple and composite keys.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Unique identifier for the relationship |
| `from` | string | Yes | The logical dataset on the many side of the relationship |
| `to` | string | Yes | The logical dataset on the one side of the relationship |
| `from_columns` | array | Yes | Array of column names in the "from" dataset (foreign key columns) |
| `to_columns` | array | Yes | Array of column names in the "to" dataset (primary or unique key columns) |
| `ai_context` | string/object | No | Additional context for AI tools |
| `custom_extensions` | array | No | Vendor-specific attributes |

The order of columns in `from_columns` must correspond to the order in `to_columns`. Both arrays must have the same number of columns.

## Fields

Fields represent row-level attributes that can be used for grouping, filtering, and in metric expressions. They can be simple column references or computed expressions.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Unique identifier for the field within the dataset |
| `expression` | object | Yes | Expression definition with dialect support |
| `dimension` | object | No | Dimension metadata (e.g., `is_time` flag) |
| `label` | string | No | Label for categorization |
| `description` | string | No | Human-readable description |
| `ai_context` | string/object | No | Additional context for AI tools (e.g., synonyms) |
| `custom_extensions` | array | No | Vendor-specific attributes |

The `expression` object holds a `dialects[]` array of `{dialect, expression}` pairs. Use scalar SQL expressions (no aggregations) — simple references like `customer_id` or computed like `first_name || ' ' || last_name`.

## Metrics

Quantitative measures defined on business data, representing key calculations like sums, averages, ratios, etc. Metrics are defined at the semantic model level and can span multiple datasets.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Unique identifier for the metric |
| `expression` | object | Yes | Expression definition with dialect support |
| `description` | string | No | Human-readable description of what the metric measures |
| `ai_context` | string/object | No | Additional context for AI tools (e.g., synonyms) |
| `custom_extensions` | array | No | Vendor-specific attributes |

## AI Context Structure

The `ai_context` field can be either a simple string or a structured object with specific keys:

```yaml
ai_context:
  instructions: "Use this for sales analysis"
  synonyms:
    - "orders"
    - "purchases"
  examples:
    - "Show total sales last month"
```

| Field | Type | Description |
|-------|------|-------------|
| `instructions` | string | Instructions for AI on how to use this entity |
| `synonyms` | array | Alternative names and terms |
| `examples` | array | Sample questions or use cases |

## Version History

- **0.1.1** (2025-12-11): Initial release
  - Core semantic model structure
  - Support for datasets, relationships, fields, and metrics
  - Multi-dialect metric expressions
  - Vendor extensibility framework
  - Context for agents
