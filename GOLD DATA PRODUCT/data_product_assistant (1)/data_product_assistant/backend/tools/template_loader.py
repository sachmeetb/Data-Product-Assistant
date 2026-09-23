"""
template_loader.py — GCS-backed industry template loader with local fallback.

Resolution order for templates:
  1. GCS bucket (gs://{GCS_TEMPLATES_BUCKET}/{industry}/{domain}.json) when
     GCS_TEMPLATES_BUCKET env var is set.
  2. backend/data/{domain}_silver_domain.json  (existing CPG flat naming)
  3. backend/data/{industry}/{domain}.json     (new subdirectory naming)

Registry resolution order:
  1. GCS bucket root (gs://{GCS_TEMPLATES_BUCKET}/registry.json)
  2. backend/data/gcs_registry.json           (local dev mirror)

Detection uses the `detection_signals` array in each registry entry.
A domain is accepted when >= _DETECTION_THRESHOLD signals are found in the
lowercased corpus built from discovery_view fields + silver_matches table names.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

_logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"
_LOCAL_REGISTRY_PATH = _DATA_DIR / "gcs_registry.json"

_DETECTION_THRESHOLD = 2


class TemplateLoader:
    def __init__(self) -> None:
        self._registry_cache: Optional[dict] = None
        self._template_cache: dict[str, dict] = {}
        self._bucket_name: str = os.environ.get("GCS_TEMPLATES_BUCKET", "")

    # ── registry ─────────────────────────────────────────────────────────────

    def load_registry(self) -> dict:
        """Load (and cache) the template registry. GCS first, local fallback."""
        if self._registry_cache is not None:
            return self._registry_cache

        if self._bucket_name:
            try:
                registry = self._gcs_download_json(f"registry.json")
                _logger.info("[template-loader] Loaded registry from GCS gs://%s/registry.json", self._bucket_name)
                self._registry_cache = registry
                return registry
            except Exception as exc:
                _logger.warning("[template-loader] GCS registry load failed (%s); falling back to local", exc)

        try:
            with open(_LOCAL_REGISTRY_PATH, encoding="utf-8") as f:
                registry = json.load(f)
            _logger.info("[template-loader] Loaded registry from local %s", _LOCAL_REGISTRY_PATH)
            self._registry_cache = registry
            return registry
        except (OSError, json.JSONDecodeError) as exc:
            _logger.error("[template-loader] Local registry load failed: %s", exc)
            return {}

    # ── detection ────────────────────────────────────────────────────────────

    def detect_template(self, discovery_output: dict) -> Optional[tuple[str, str]]:
        """
        Score all registry entries against the discovery output corpus.

        Returns (industry, domain) for the entry with the highest signal hit count
        that also meets _DETECTION_THRESHOLD, or None if no entry qualifies.

        Uses discovery_view.domain as a tiebreaker / pre-filter when set.
        """
        registry = self.load_registry()
        industries: dict = registry.get("industries", {})
        if not industries:
            return None

        corpus = self._build_corpus(discovery_output)
        if not corpus:
            return None

        domain_hint = str(
            (discovery_output.get("discovery_view") or {}).get("domain", "")
        ).lower()

        best_score = 0
        best_match: Optional[tuple[str, str]] = None

        for industry_key, domains in industries.items():
            for domain_key, entry in domains.items():
                signals: list[str] = entry.get("detection_signals", [])
                score = sum(1 for sig in signals if sig.lower() in corpus)

                if score < _DETECTION_THRESHOLD:
                    continue

                # Prefer a match that aligns with the explicit domain hint
                hint_bonus = 1 if domain_hint and domain_hint in (domain_key, industry_key) else 0
                effective = score + hint_bonus

                if effective > best_score:
                    best_score = effective
                    best_match = (industry_key, domain_key)

        if best_match:
            _logger.info(
                "[template-loader] Detected domain=%s/%s (score=%d)",
                best_match[0], best_match[1], best_score,
            )
        return best_match

    def _build_corpus(self, discovery_output: dict) -> str:
        view: dict = discovery_output.get("discovery_view") or {}

        parts = [
            str(view.get("domain", "")),
            str(view.get("use_case", "")),
            " ".join(str(k) for k in (view.get("kpis") or [])),
            " ".join(str(d) for d in (view.get("dimensions") or [])),
        ]

        for match_key in ("silver_matches", "bronze_matches", "gold_matches"):
            for m in (discovery_output.get(match_key) or []):
                parts.append(str(m.get("name", "") or m.get("table_name", "")))
                tags = m.get("tags") or {}
                parts.append(str(tags.get("domain", "")))

        return " ".join(parts).lower()

    # ── template loading ──────────────────────────────────────────────────────

    def load_template(self, industry: str, domain: str) -> dict:
        """
        Load a specific template by (industry, domain).

        Caches after first load.  Raises FileNotFoundError if not found anywhere.
        """
        cache_key = f"{industry}/{domain}"
        if cache_key in self._template_cache:
            return self._template_cache[cache_key]

        if self._bucket_name:
            gcs_path = f"{industry}/{domain}.json"
            try:
                template = self._gcs_download_json(gcs_path)
                _logger.info("[template-loader] Loaded %s/%s from GCS", industry, domain)
                self._template_cache[cache_key] = template
                return template
            except Exception as exc:
                _logger.warning("[template-loader] GCS template %s failed (%s); trying local", gcs_path, exc)

        # Local fallback 1: legacy flat naming (CPG only)
        legacy_path = _DATA_DIR / f"{domain}_silver_domain.json"
        if legacy_path.exists():
            template = self._load_local(legacy_path)
            _logger.info("[template-loader] Loaded %s from local legacy path", legacy_path.name)
            self._template_cache[cache_key] = template
            return template

        # Local fallback 2: new subdirectory naming
        subdir_path = _DATA_DIR / industry / f"{domain}.json"
        if subdir_path.exists():
            template = self._load_local(subdir_path)
            _logger.info("[template-loader] Loaded %s/%s from local subdir", industry, domain)
            self._template_cache[cache_key] = template
            return template

        raise FileNotFoundError(
            f"Template not found for {industry}/{domain}. "
            f"Tried GCS, {legacy_path}, {subdir_path}."
        )

    def detect_and_load(self, discovery_output: dict) -> Optional[dict]:
        """
        Convenience: detect domain then load its template.

        Returns None on any miss or load failure (graceful degradation).
        """
        match = self.detect_template(discovery_output)
        if match is None:
            return None
        industry, domain = match
        try:
            return self.load_template(industry, domain)
        except (FileNotFoundError, Exception) as exc:
            _logger.warning("[template-loader] Failed to load template %s/%s: %s", industry, domain, exc)
            return None

    # ── GCS helpers ──────────────────────────────────────────────────────────

    def _gcs_download_json(self, blob_name: str) -> dict:
        from google.cloud import storage  # lazy import — avoids hard dep in local dev

        client = storage.Client()
        bucket = client.bucket(self._bucket_name)
        blob = bucket.blob(blob_name)
        raw = blob.download_as_text(encoding="utf-8")
        return json.loads(raw)

    @staticmethod
    def _load_local(path: Path) -> dict:
        with open(path, encoding="utf-8") as f:
            return json.load(f)


# Module-level singleton used by silver_layer_agent.py
_loader = TemplateLoader()
