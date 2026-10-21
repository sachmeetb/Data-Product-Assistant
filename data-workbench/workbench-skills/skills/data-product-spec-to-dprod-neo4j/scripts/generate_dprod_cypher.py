#!/usr/bin/env python3
"""
Generate DPROD-aligned Cypher from a data product spec YAML file.

Reads a spec produced by the data-product-spec-writer skill and generates
Cypher CREATE statements for DPROD (Data Product Ontology) nodes and
relationships in Neo4j.

DPROD ontology reference: https://ekgf.github.io/dprod/

DCAT/DPROD separation:
  Source datasets and product output datasets are DISTINCT nodes:

    InputPort  -[:DPROD_INPUT_DATASET]->  :Dataset           (existing DCAT node)
    OutputPort -[:DPROD_OUTPUT_DATASET]-> :DProdOutputDataset (new product-contract node)
    :DProdOutputDataset -[:DERIVED_FROM]-> :Dataset           (source-aligned lineage)

  This correctly models:
    - What flows IN  (InputPort → source :Dataset from DCAT discovery)
    - What is served OUT (OutputPort → product-level :DProdOutputDataset)
    - The lineage link connecting product output back to source

  Existing :NodeShape nodes (DQ rules) are still linked to the output port as
  quality shapes without being recreated.

Usage:
    python generate_dprod_cypher.py <spec_yaml_file> [output_file]

output_file defaults to <spec_yaml_stem>_dprod.cypher in the same directory.
"""
import sys
import re
import argparse
from datetime import datetime, timezone
from pathlib import Path


def install_deps():
    import importlib
    try:
        importlib.import_module('yaml')
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'pyyaml', '-q'])


install_deps()
import yaml  # noqa: E402


# ── Cypher helpers ────────────────────────────────────────────────────────────

def _esc(s) -> str:
    """Escape a value for a Cypher single-quoted string literal."""
    if s is None:
        return 'null'
    return "'" + str(s).replace('\\', '\\\\').replace("'", "\\'") + "'"


def _list(lst) -> str:
    if not lst:
        return '[]'
    items = [_esc(str(i)) for i in lst if i is not None]
    return '[' + ', '.join(items) + ']'


def _props(d: dict) -> str:
    """Render a dict as Cypher property map, one entry per line."""
    lines = []
    for k, v in d.items():
        if v is None:
            lines.append(f'  {k}: null')
        elif isinstance(v, bool):
            lines.append(f'  {k}: {str(v).lower()}')
        elif isinstance(v, (int, float)):
            lines.append(f'  {k}: {v}')
        elif isinstance(v, list):
            lines.append(f'  {k}: {_list(v)}')
        else:
            lines.append(f'  {k}: {_esc(str(v))}')
    return '{\n' + ',\n'.join(lines) + '\n}'


def _create(label: str, props: dict) -> str:
    return f'CREATE (:{label} {_props(props)});'


def _match_create_rel(
    from_label: str, from_match: dict,
    rel: str,
    to_label: str, to_match: dict,
) -> str:
    from_props = ', '.join(f'{k}: {_esc(v)}' for k, v in from_match.items())
    to_props = ', '.join(f'{k}: {_esc(v)}' for k, v in to_match.items())
    return (
        f'MATCH (a:{from_label} {{{from_props}}})\n'
        f'MATCH (b:{to_label} {{{to_props}}})\n'
        f'CREATE (a)-[:{rel}]->(b);'
    )


# ── Section generators ────────────────────────────────────────────────────────

def gen_lifecycle_status_nodes() -> list[str]:
    """MERGE shared lifecycle status nodes — idempotent."""
    stmts = ['// ── Lifecycle status nodes (MERGE — shared, idempotent) ──────────────────────']
    for status in ('draft', 'active', 'deprecated'):
        props = {
            'uri': f'dprod:lifecycle:{status}',
            'name': status,
            'dcatType': 'dprod:DataProductLifecycleStatus',
        }
        p = ', '.join(f'{k}: {_esc(v)}' for k, v in props.items())
        stmts.append(f'MERGE (:DProdLifecycleStatus {{{p}}});')
    return stmts


def gen_data_product_node(p: dict) -> list[str]:
    product_id = p['id']
    purpose = p.get('purpose', {}) or {}
    quality = p.get('quality', {}) or {}
    ops = p.get('operations', {}) or {}

    stmts = ['// ── DProdDataProduct node ───────────────────────────────────────────────────']
    props = {
        'uri': f'dprod:{product_id}',
        'id': product_id,
        'name': p.get('name'),
        'domain': p.get('domain'),
        'type': p.get('type', 'source-aligned'),
        'version': p.get('versioning', {}).get('version', '1.0.0') if p.get('versioning') else '1.0.0',
        'status': p.get('status', 'draft'),
        'owner': p.get('owner'),
        'stewardTeam': p.get('steward_team'),
        'contactChannel': p.get('contact_channel'),
        'businessPurpose': str(purpose.get('business_purpose', '')).strip() or None,
        'primaryConsumers': [c for c in (purpose.get('primary_consumers') or []) if c and c != 'TODO'],
        'keyUseCases': [u for u in (purpose.get('key_use_cases') or []) if u and u != 'TODO'],
        'nonGoals': [g for g in (purpose.get('non_goals') or []) if g and g != 'TODO'],
        'overallQualitySla': quality.get('overall_sla'),
        'issueHandling': quality.get('issue_handling'),
        'testExecutionPoint': quality.get('test_execution_point'),
        'retention': ops.get('retention'),
        'refreshPattern': ops.get('refresh_pattern'),
        'latencyTargetMinutes': ops.get('latency_target_minutes'),
        'availabilitySla': ops.get('availability_sla'),
        'dcatType': 'dprod:DataProduct',
    }
    stmts.append(_create('DProdDataProduct', props))
    return stmts


def gen_lifecycle_rel(product_id: str, status: str) -> list[str]:
    stmts = ['// ── Lifecycle status relationship ───────────────────────────────────────────']
    stmts.append(
        f"MATCH (dp:DProdDataProduct {{uri: {_esc('dprod:' + product_id)}}})\n"
        f"MATCH (ls:DProdLifecycleStatus {{name: {_esc(status)}}})\n"
        f"CREATE (dp)-[:DPROD_LIFECYCLE_STATUS]->(ls);"
    )
    return stmts


def gen_registry_link(product_id: str) -> list[str]:
    """Link DProdDataProduct to the existing :DataProduct registry node."""
    stmts = ['// ── Link to registry :DataProduct node ─────────────────────────────────────']
    stmts.append(
        f"MATCH (dp:DProdDataProduct {{uri: {_esc('dprod:' + product_id)}}})\n"
        f"MATCH (reg:DataProduct {{id: {_esc(product_id)}}})\n"
        f"CREATE (dp)-[:REGISTERED_AS]->(reg);"
    )
    return stmts


def gen_input_ports(product_id: str, lineage: dict) -> list[str]:
    stmts = ['// ── InputPort node(s) ───────────────────────────────────────────────────────']
    sources = lineage.get('upstream_systems') or [{}]
    pipelines = lineage.get('ingestion_pipelines') or [{}]
    transforms = lineage.get('transformations_applied') or []

    # Filter out pure TODO/null entries
    real_sources = [s for s in sources if s and any(v and v != 'TODO' for v in s.values())]
    if not real_sources:
        real_sources = [{}]  # always create at least one input port placeholder

    for i, source in enumerate(real_sources):
        pipe = pipelines[i] if i < len(pipelines) else {}
        port_uri = f'dprod:{product_id}:inputPort:{i}'
        source_name = source.get('name') if source.get('name') not in (None, 'TODO') else None
        source_owner = source.get('owner') if source.get('owner') not in (None, 'TODO') else None
        pipe_name = pipe.get('name') if pipe and pipe.get('name') not in (None, 'TODO') else None
        pipe_repo = pipe.get('repo') if pipe else None
        pipe_orch = pipe.get('orchestration_id') if pipe else None

        real_transforms = [t for t in transforms if t and t != 'TODO']

        props = {
            'uri': port_uri,
            'name': f'source-{i}' if not source_name else source_name,
            'sourceName': source_name,
            'sourceOwner': source_owner,
            'ingestionPipeline': pipe_name,
            'pipelineRepo': pipe_repo,
            'orchestrationId': pipe_orch,
            'transformationsApplied': real_transforms,
            'dcatType': 'dprod:InputPort',
        }
        stmts.append(_create('DProdInputPort', props))
        stmts.append(
            f"MATCH (dp:DProdDataProduct {{uri: {_esc('dprod:' + product_id)}}})\n"
            f"MATCH (ip:DProdInputPort {{uri: {_esc(port_uri)}}})\n"
            f"CREATE (dp)-[:DPROD_INPUT_PORT]->(ip);"
        )
    return stmts


def gen_output_port(product_id: str, p: dict) -> list[str]:
    stmts = ['// ── OutputPort node ─────────────────────────────────────────────────────────']
    ops = p.get('operations', {}) or {}
    access = p.get('access', {}) or {}
    enforcement = access.get('enforcement', {}) or {}
    masking = access.get('masking_rules') or []
    real_masks = [m for m in masking if m and any(
        v and v != 'TODO' for v in (m.values() if isinstance(m, dict) else [])
    )]

    port_uri = f'dprod:{product_id}:outputPort:main'
    props = {
        'uri': port_uri,
        'name': 'main',
        'platform': ops.get('platform_location'),
        'classification': access.get('classification'),
        'accessModel': access.get('access_model'),
        'enforcementPlatform': enforcement.get('platform'),
        'enforcementRoles': enforcement.get('roles') or [],
        'complianceConstraints': [c for c in (access.get('compliance_constraints') or []) if c and c != 'TODO'],
        'restrictions': [r for r in (access.get('restrictions') or []) if r and r != 'TODO'],
        'hasMaskingRules': len(real_masks) > 0,
        'dcatType': 'dprod:OutputPort',
    }
    stmts.append(_create('DProdOutputPort', props))
    stmts.append(
        f"MATCH (dp:DProdDataProduct {{uri: {_esc('dprod:' + product_id)}}})\n"
        f"MATCH (op:DProdOutputPort {{uri: {_esc(port_uri)}}})\n"
        f"CREATE (dp)-[:DPROD_OUTPUT_PORT]->(op);"
    )
    return stmts


def gen_output_datasets(product_id: str, tables: list) -> list[str]:
    """
    Create :DProdOutputDataset nodes (product-contract level) and wire them up:
      (:DProdOutputPort)-[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
      (:DProdOutputDataset)-[:DERIVED_FROM]->(:Dataset)   ← source-aligned lineage

    :DProdOutputDataset is DISTINCT from the DCAT :Dataset nodes already in the
    graph. It represents what the product *promises* to serve; :Dataset represents
    what was *discovered* in the source database.
    """
    stmts = [
        '// ── DProdOutputDataset nodes (product contract — distinct from source :Dataset) ─',
    ]
    port_uri = f'dprod:{product_id}:outputPort:main'
    for table in tables:
        tname = table.get('name')
        if not tname:
            continue
        out_ds_uri = f'dprod:{product_id}:outputDataset:{tname}'
        props = {
            'uri': out_ds_uri,
            'name': tname,
            'label': table.get('label') or tname,
            'grain': table.get('grain'),
            'primaryKey': table.get('primary_key') or [],
            'rowCount': table.get('row_count'),
            'dcatType': 'dprod:OutputDataset',
        }
        # 1. Create the product-level output dataset node
        stmts.append(_create('DProdOutputDataset', props))
        # 2. Link output port → product output dataset
        stmts.append(
            f"MATCH (op:DProdOutputPort {{uri: {_esc(port_uri)}}})\n"
            f"MATCH (ods:DProdOutputDataset {{uri: {_esc(out_ds_uri)}}})\n"
            f"CREATE (op)-[:DPROD_OUTPUT_DATASET]->(ods);"
        )
        # 3. Source-aligned lineage: product output dataset → source DCAT :Dataset
        stmts.append(
            f"MATCH (ods:DProdOutputDataset {{uri: {_esc(out_ds_uri)}}})\n"
            f"MATCH (:DataProduct {{id: {_esc(product_id)}}})-[:INCLUDES_DATASET]->(ds:Dataset {{name: {_esc(tname)}}})\n"
            f"CREATE (ods)-[:DERIVED_FROM]->(ds);"
        )
    return stmts


def gen_input_dataset_links(product_id: str, tables: list) -> list[str]:
    """
    Link InputPort(s) to existing DCAT :Dataset nodes (source side).
    The InputPort represents ingestion from upstream source systems — it points
    to the original :Dataset nodes (not to the product-level output datasets).
    All tables are linked to inputPort:0 (the primary source system).
    """
    stmts = [
        '// ── InputPort → source :Dataset links (DCAT source nodes) ─────────────────',
    ]
    port_uri = f'dprod:{product_id}:inputPort:0'
    for table in tables:
        tname = table.get('name')
        if not tname:
            continue
        stmts.append(
            f"MATCH (ip:DProdInputPort {{uri: {_esc(port_uri)}}})\n"
            f"MATCH (:DataProduct {{id: {_esc(product_id)}}})-[:INCLUDES_DATASET]->(ds:Dataset {{name: {_esc(tname)}}})\n"
            f"CREATE (ip)-[:DPROD_INPUT_DATASET]->(ds);"
        )
    return stmts


def gen_quality_shape_links(product_id: str) -> list[str]:
    """
    Link OutputPort to existing :NodeShape nodes (DQ rules) already in the graph.
    One statement links all shapes scoped to this product via the registry.
    """
    port_uri = f'dprod:{product_id}:outputPort:main'
    stmts = [
        '// ── OutputPort → :NodeShape links (reuses existing SHACL rule containers) ─',
    ]
    stmts.append(
        f"MATCH (op:DProdOutputPort {{uri: {_esc(port_uri)}}})\n"
        f"MATCH (:DataProduct {{id: {_esc(product_id)}}})-[:INCLUDES_DATASET]->(:Dataset)-[:HAS_SHAPE]->(ns:NodeShape)\n"
        f"CREATE (op)-[:HAS_QUALITY_SHAPE]->(ns);"
    )
    return stmts


def gen_catalog_link(product_id: str) -> list[str]:
    """Link OutputPort to the :Catalog (source schema) via the registry node."""
    port_uri = f'dprod:{product_id}:outputPort:main'
    stmts = ['// ── OutputPort → :Catalog link ──────────────────────────────────────────────']
    stmts.append(
        f"MATCH (op:DProdOutputPort {{uri: {_esc(port_uri)}}})\n"
        f"MATCH (:DataProduct {{id: {_esc(product_id)}}})-[:FROM_CATALOG]->(cat:Catalog)\n"
        f"CREATE (op)-[:FROM_CATALOG]->(cat);"
    )
    return stmts


def gen_version_nodes(product_id: str, versioning: dict) -> list[str]:
    if not versioning:
        return []
    stmts = ['// ── Version node(s) ─────────────────────────────────────────────────────────']
    changelog = versioning.get('changelog') or []
    bcp = str(versioning.get('breaking_change_policy', '') or '').strip() or None
    depr = versioning.get('deprecation_strategy')
    planned = [c for c in (versioning.get('planned_changes') or []) if c and c != 'TODO']

    for entry in changelog:
        ver = entry.get('version', '1.0.0')
        ver_uri = f'dprod:{product_id}:version:{ver}'
        props = {
            'uri': ver_uri,
            'version': ver,
            'date': entry.get('date'),
            'description': entry.get('description'),
            'breakingChangePolicy': bcp,
            'deprecationStrategy': depr,
            'plannedChanges': planned,
            'dcatType': 'dprod:Version',
        }
        stmts.append(_create('DProdVersion', props))
        stmts.append(
            f"MATCH (dp:DProdDataProduct {{uri: {_esc('dprod:' + product_id)}}})\n"
            f"MATCH (v:DProdVersion {{uri: {_esc(ver_uri)}}})\n"
            f"CREATE (dp)-[:DPROD_HAS_VERSION]->(v);"
        )
    return stmts


def gen_access_policy(product_id: str, access: dict) -> list[str]:
    if not access:
        return []
    stmts = ['// ── AccessPolicy node ───────────────────────────────────────────────────────']
    enforcement = access.get('enforcement', {}) or {}
    masking = access.get('masking_rules') or []
    # Serialize masking rules as a list of strings
    masking_strs = []
    for m in masking:
        if not m or not isinstance(m, dict):
            continue
        col = m.get('column', '')
        rule = m.get('rule')
        cls = m.get('classification', '')
        if col and col != 'TODO':
            desc = f'{col} ({cls})' + (f': {rule}' if rule and rule != 'TODO' else '')
            masking_strs.append(desc)

    policy_uri = f'dprod:{product_id}:access'
    props = {
        'uri': policy_uri,
        'classification': access.get('classification'),
        'accessModel': access.get('access_model'),
        'enforcementPlatform': enforcement.get('platform'),
        'enforcementRoles': enforcement.get('roles') or [],
        'maskingRules': masking_strs,
        'complianceConstraints': [c for c in (access.get('compliance_constraints') or []) if c and c != 'TODO'],
        'restrictions': [r for r in (access.get('restrictions') or []) if r and r != 'TODO'],
        'dcatType': 'dprod:AccessPolicy',
    }
    stmts.append(_create('DProdAccessPolicy', props))
    stmts.append(
        f"MATCH (dp:DProdDataProduct {{uri: {_esc('dprod:' + product_id)}}})\n"
        f"MATCH (ap:DProdAccessPolicy {{uri: {_esc(policy_uri)}}})\n"
        f"CREATE (dp)-[:HAS_ACCESS_POLICY]->(ap);"
    )
    return stmts


# ── Main ──────────────────────────────────────────────────────────────────────

def generate(spec_path: Path, output_path: Path) -> dict:
    with open(spec_path) as fh:
        doc = yaml.safe_load(fh)

    p = doc.get('product', doc)  # handle both top-level 'product:' key and bare dict
    product_id = p['id']
    status = p.get('status', 'draft')
    tables = p.get('tables') or []
    lineage = p.get('lineage', {}) or {}
    versioning = p.get('versioning', {}) or {}
    access = p.get('access', {}) or {}

    sections = [
        [
            f'// DPROD Knowledge Graph Enrichment',
            f'// Ontology: https://ekgf.github.io/dprod/',
            f'// Product: {product_id}',
            f'// Spec: {spec_path.name}',
            f'// Generated by data-product-spec-to-dprod-neo4j skill',
            f'// Generated at: {datetime.now(timezone.utc).replace(microsecond=0).isoformat()}',
        ],
        gen_lifecycle_status_nodes(),
        gen_data_product_node(p),
        gen_lifecycle_rel(product_id, status),
        gen_registry_link(product_id),
        gen_input_ports(product_id, lineage),
        gen_input_dataset_links(product_id, tables),
        gen_output_port(product_id, p),
        gen_output_datasets(product_id, tables),
        gen_quality_shape_links(product_id),
        gen_catalog_link(product_id),
        gen_version_nodes(product_id, versioning),
        gen_access_policy(product_id, access),
    ]

    lines = []
    for section in sections:
        lines.extend(section)
        lines.append('')

    output_path.write_text('\n'.join(lines))

    # Count statements
    n_stmts = sum(1 for l in lines if l.strip().endswith(';'))
    n_merges = sum(1 for l in lines if l.strip().startswith('MERGE'))
    n_creates = sum(1 for l in lines if l.strip().startswith('CREATE') and ';' in l)
    n_rels = sum(1 for l in lines if '-[:' in l and l.strip().endswith(';'))

    return {
        'product_id': product_id,
        'output_path': str(output_path),
        'total_statements': n_stmts,
        'merge_statements': n_merges,
        'create_statements': n_creates,
        'relationship_statements': n_rels,
        'tables': len(tables),
    }


def main():
    parser = argparse.ArgumentParser(
        description='Generate DPROD-aligned Cypher from a data product spec YAML'
    )
    parser.add_argument('spec_yaml', help='Path to the data product spec YAML file')
    parser.add_argument('output_file', nargs='?', default=None,
                        help='Output Cypher file (default: <spec_stem>_dprod.cypher)')
    args = parser.parse_args()

    spec_path = Path(args.spec_yaml)
    if not spec_path.exists():
        print(f'ERROR: spec file not found: {spec_path}', file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output_file) if args.output_file else \
        spec_path.parent / f'{spec_path.stem}_dprod.cypher'

    stats = generate(spec_path, output_path)
    print(f'Generated: {stats["output_path"]}', file=sys.stderr)
    print(f'  Product:    {stats["product_id"]}', file=sys.stderr)
    print(f'  Tables:     {stats["tables"]}', file=sys.stderr)
    print(f'  Statements: {stats["total_statements"]} '
          f'(MERGE: {stats["merge_statements"]}, '
          f'CREATE node/rel: {stats["create_statements"]})',
          file=sys.stderr)


if __name__ == '__main__':
    main()
