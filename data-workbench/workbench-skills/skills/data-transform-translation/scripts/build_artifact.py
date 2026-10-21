#!/usr/bin/env python3
"""Build step: compile the per-platform authoring YAML into ONE checksummed
runtime artifact the backend reads.

Fail-closed: validates every source YAML against
``schema/transforms_source.schema.json``, merges + inverts them, stamps
``schema_version`` + a content ``checksum``, then validates the RESULT against
``schema/transform_capabilities.schema.json``. On any error it writes nothing.

Usage:
    python build_artifact.py --out ../../../workbench/backend/platform/transform_capabilities.v1.json
    python build_artifact.py --check   # build in-memory + validate, write nothing (CI/pre-commit)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml is required (pip install pyyaml).", file=sys.stderr)
    sys.exit(2)

try:
    import jsonschema
except ImportError:
    print("ERROR: jsonschema is required (pip install jsonschema).", file=sys.stderr)
    sys.exit(2)

SKILL_DIR = Path(__file__).resolve().parent.parent
REFERENCE_DIR = SKILL_DIR / "reference"
SCHEMA_DIR = SKILL_DIR / "schema"

SCHEMA_VERSION_DEFAULT = "v1"
GENERATOR = "data-transform-translation/scripts/build_artifact.py"
SOURCE_SKILL = "data-transform-translation"

# The served platforms this corpus MUST cover. A missing reference/<p>/transforms.yaml
# aborts the build — the artifact is only useful if every served platform is present.
REQUIRED_PLATFORMS = ["postgres", "databricks", "snowflake", "bigquery", "mysql"]


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level, got {type(data).__name__}")
    return data


def _load_schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def canonical_checksum(artifact: dict) -> str:
    """sha256 over the canonical JSON of the artifact body with the volatile
    ``checksum`` + ``generated_at`` fields removed. Shared verbatim with the
    backend loader so a fork's regenerated artifact re-verifies deterministically.
    """
    body = {k: v for k, v in artifact.items() if k not in ("checksum", "generated_at")}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()


def build(schema_version: str = SCHEMA_VERSION_DEFAULT) -> dict:
    source_schema = _load_schema("transforms_source.schema.json")

    # Portable functions (ANSI-safe on every served platform).
    portable_doc = _load_yaml(REFERENCE_DIR / "portable_functions.yaml")
    portable = sorted({str(f).upper() for f in portable_doc.get("portable_functions", [])})
    if not portable:
        raise ValueError("portable_functions.yaml declared no functions.")

    functions: dict[str, dict] = {}
    ops: dict[str, dict] = {}

    for platform in REQUIRED_PLATFORMS:
        src_path = REFERENCE_DIR / platform / "transforms.yaml"
        if not src_path.exists():
            raise FileNotFoundError(f"Missing required source for served platform {platform!r}: {src_path}")
        doc = _load_yaml(src_path)
        jsonschema.validate(doc, source_schema)  # raises on any violation
        if doc["platform"] != platform:
            raise ValueError(f"{src_path}: declares platform={doc['platform']!r} but lives under {platform}/")

        for fname, entry in (doc.get("functions") or {}).items():
            key = fname.upper()
            if key in portable:
                raise ValueError(
                    f"{src_path}: function {key!r} is in portable_functions.yaml; "
                    f"a per-platform entry contradicts the portable claim. Remove one."
                )
            functions.setdefault(key, {"platforms": {}})
            if platform in functions[key]["platforms"]:
                raise ValueError(f"Duplicate entry for {key!r} on {platform!r}.")
            functions[key]["platforms"][platform] = _clean_entry(entry, doc["engine"])

        for op_name, variants in (doc.get("ops") or {}).items():
            ops.setdefault(op_name, {"semantics": {}})
            for variant, entry in variants.items():
                ops[op_name]["semantics"].setdefault(variant, {})
                ops[op_name]["semantics"][variant][platform] = _clean_entry(entry, doc["engine"])

    artifact = {
        "schema_version": schema_version,
        "checksum": "",  # filled below
        "generated_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator": GENERATOR,
        "source_skill": SOURCE_SKILL,
        "platforms": list(REQUIRED_PLATFORMS),
        "portable_functions": portable,
        "functions": dict(sorted(functions.items())),
        "ops": dict(sorted(ops.items())),
    }
    artifact["checksum"] = canonical_checksum(artifact)

    # Validate the RESULT against the artifact schema — fail closed.
    jsonschema.validate(artifact, _load_schema("transform_capabilities.schema.json"))
    return artifact


def _clean_entry(entry: dict, file_engine: str) -> dict:
    """Normalize a source entry into the artifact entry shape (default status +
    inherit file-level engine when the entry doesn't override it)."""
    out = dict(entry)
    out.setdefault("status", "conformance-verify")
    out.setdefault("engine", file_engine)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the transform capability runtime artifact.")
    ap.add_argument("--out", type=Path, default=None, help="Path to write the JSON artifact.")
    ap.add_argument("--schema-version", default=SCHEMA_VERSION_DEFAULT)
    ap.add_argument("--check", action="store_true", help="Build + validate in memory, write nothing.")
    args = ap.parse_args(argv)

    try:
        artifact = build(args.schema_version)
    except Exception as e:  # noqa: BLE001 — surface any authoring/schema error, write nothing
        print(f"BUILD FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    fn_count = len(artifact["functions"])
    op_count = sum(len(v["semantics"]) for v in artifact["ops"].values())
    print(
        f"OK: schema={artifact['schema_version']} checksum={artifact['checksum'][:23]}… "
        f"platforms={len(artifact['platforms'])} portable={len(artifact['portable_functions'])} "
        f"functions={fn_count} op-variants={op_count}",
        file=sys.stderr,
    )

    if args.check:
        return 0
    if args.out is None:
        json.dump(artifact, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"WROTE {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
