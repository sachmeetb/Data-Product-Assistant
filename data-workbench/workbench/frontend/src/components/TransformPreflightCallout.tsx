// Transform capability preflight callout (Phase 5 of transform-portability.md).
//
// Mounted at the top of MappingReviewPanel. Fetches
//   GET /api/projects/{id}/serving/transform-preflight
// which compiles every mapping's transform expression against the RESOLVED
// serving platform (e.g. Databricks for a consumer over a materialized source)
// and returns a CompileResult. This surfaces the "AGE() is unsupported on
// databricks" class of finding — with the fix — at AUTHOR time, in the mapping
// editor, instead of only at deploy.
//
// Auto-fires on mount (and when `refreshKey` changes, e.g. after a mapping
// edit). Fail-quiet: a fetch error (no serving context yet, Neo4j down) renders
// nothing rather than a scary banner.

import { useCallback, useEffect, useState, type CSSProperties } from "react";
import api from "../api/client";

interface Diagnostic {
  severity: string;
  code: string;
  message: string;
  function?: string;
  remediation?: string;
  citation?: string;
  product_col?: string;
  mapping_uri?: string;
}

interface PreflightResult {
  ok: boolean;
  validated: boolean;
  platform: string;
  view_schema: string;
  // True when `platform` is one of the five served platforms that ship a
  // capability profile (postgres/databricks/snowflake/bigquery/mysql); false
  // for the profileless fallback dialects (ansi/duckdb).
  platform_has_profile?: boolean;
  errors: Diagnostic[];
  warnings: Diagnostic[];
  used_capabilities: string[];
}

interface Props {
  projectId: number;
  refreshKey?: number;
}

export default function TransformPreflightCallout({ projectId, refreshKey }: Props) {
  const [result, setResult] = useState<PreflightResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const [collapsed, setCollapsed] = useState(false);

  const run = useCallback(async () => {
    setLoading(true);
    setFailed(false);
    try {
      const res = await api.get(`/api/projects/${projectId}/serving/transform-preflight`);
      setResult(res.data as PreflightResult);
    } catch {
      // No serving context / graph unreachable — stay quiet, this is advisory.
      setResult(null);
      setFailed(true);
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    run();
  }, [run, refreshKey]);

  if (failed) return null;
  if (loading && !result) {
    return <div style={styles.muted}>Checking transform portability…</div>;
  }
  if (!result) return null;

  const errors = result.errors || [];
  const warnings = result.warnings || [];

  // Validation was skipped — a compact grey note. "skipped" is NOT "clean".
  // Two distinct reasons, told apart by `platform_has_profile`:
  //  - profiled platform (postgres/databricks/snowflake/bigquery/mysql) but
  //    nothing compilable to check yet (mappings unapproved / serving unconfigured);
  //  - a fallback dialect (ansi/duckdb) that genuinely ships no capability profile.
  if (!result.validated) {
    return (
      <div style={styles.muted}>
        {result.platform_has_profile ? (
          <>
            Transform portability check hasn't run yet — no compilable transforms for{" "}
            <code style={styles.code}>{result.platform}</code> at this stage. It re-checks
            once serving is configured and mappings are approved.
          </>
        ) : (
          <>
            Transform portability check skipped — platform{" "}
            <code style={styles.code}>{result.platform}</code> has no capability profile.
          </>
        )}
      </div>
    );
  }

  // Clean.
  if (errors.length === 0 && warnings.length === 0) {
    return (
      <div style={{ ...styles.banner, ...styles.ok }}>
        <span style={{ fontWeight: 700 }}>✓ Transforms portable</span> to{" "}
        <code style={styles.code}>{result.platform}</code>. All expressions compile
        against the capability catalog.
      </div>
    );
  }

  const tone = errors.length > 0 ? styles.err : styles.warn;
  const headTone = errors.length > 0 ? "#991b1b" : "#92400e";

  return (
    <div style={{ ...styles.banner, ...tone }}>
      <div
        style={{ display: "flex", alignItems: "center", justifyContent: "space-between", cursor: "pointer" }}
        onClick={() => setCollapsed((c) => !c)}
      >
        <div style={{ fontWeight: 700, color: headTone }}>
          {errors.length > 0
            ? `⚠ ${errors.length} transform${errors.length === 1 ? "" : "s"} won't compile on ${result.platform}`
            : `${warnings.length} transform warning${warnings.length === 1 ? "" : "s"} on ${result.platform}`}
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); run(); }}
            disabled={loading}
            style={styles.recheck}
            title="Re-run the capability preflight"
          >
            {loading ? "…" : "Re-check"}
          </button>
          <span style={{ fontSize: 11, color: "#64748b" }}>{collapsed ? "▸" : "▾"}</span>
        </div>
      </div>

      {!collapsed && (
        <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 6 }}>
          {[...errors, ...warnings].map((d, i) => (
            <div key={i} style={styles.row}>
              <div style={{ fontSize: 12, color: "#0f172a" }}>
                {d.product_col && <code style={styles.code}>{d.product_col}</code>}
                {d.function && (
                  <>
                    {" "}
                    <code style={styles.code}>{d.function}()</code>
                  </>
                )}
                <span style={{ color: d.severity === "error" ? "#991b1b" : "#92400e", marginLeft: 4 }}>
                  {d.message}
                </span>
              </div>
              {d.remediation && (
                <div style={{ fontSize: 11, color: "#0f172a", marginTop: 3, fontStyle: "italic" }}>
                  Fix: {d.remediation}
                </div>
              )}
            </div>
          ))}
          <div style={{ fontSize: 11, color: "#64748b", marginTop: 2 }}>
            Re-author the mapping (Replace) so the expression uses a{" "}
            <code style={styles.code}>{result.platform}</code>-supported form, then Re-check.
          </div>
        </div>
      )}
    </div>
  );
}

const styles: Record<string, CSSProperties> = {
  banner: {
    padding: "10px 12px",
    borderRadius: 6,
    fontSize: 12,
    marginBottom: 8,
  },
  ok: { backgroundColor: "#f0fdf4", border: "1px solid #bbf7d0", color: "#166534" },
  warn: { backgroundColor: "#fffbeb", border: "1px solid #fde68a", color: "#92400e" },
  err: { backgroundColor: "#fef2f2", border: "1px solid #fecaca", color: "#991b1b" },
  muted: {
    padding: "6px 10px",
    borderRadius: 6,
    fontSize: 11,
    color: "#64748b",
    backgroundColor: "#f8fafc",
    border: "1px dashed #e2e8f0",
    marginBottom: 8,
  },
  row: {
    padding: 8,
    backgroundColor: "rgba(255,255,255,0.6)",
    border: "1px solid rgba(0,0,0,0.06)",
    borderRadius: 6,
  },
  code: {
    padding: "1px 5px",
    borderRadius: 4,
    backgroundColor: "#f1f5f9",
    color: "#0f172a",
    fontSize: 11,
    fontFamily: "ui-monospace, SFMono-Regular, monospace",
  },
  recheck: {
    padding: "2px 8px",
    borderRadius: 4,
    border: "1px solid #cbd5e1",
    backgroundColor: "#fff",
    color: "#334155",
    fontSize: 11,
    fontWeight: 600,
    cursor: "pointer",
  },
};
