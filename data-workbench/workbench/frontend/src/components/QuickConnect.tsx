import { useEffect, useState } from "react";
import api from "../api/client";

// A sample DB launched by the `dwb` CLI, surfaced by the backend dev router
// (GET /api/dev/provisioned-sources) for one-click form prefill. Demo creds only.
export interface ProvisionedSource {
  name: string;
  platform: string;
  host: string;
  port: number;
  database: string;
  username: string;
  password: string;
  schema: string;
  sample?: string;
  mode?: string;
}

interface Props {
  /** When set, only sources on this platform are offered (the data-source
   *  dialog is postgres-only; the registry form takes any platform). */
  platform?: string;
  /** Fill the parent form from the chosen source. */
  onPick: (s: ProvisionedSource) => void;
  /** Re-fetch whenever this changes (e.g. dialog `open`). */
  refreshKey?: unknown;
}

/**
 * Collapsible "Quick connect" section. Fetches the CLI-provisioned sample DBs
 * and renders a button per source that prefills the connection form. Renders
 * nothing when no manifest exists (the common case outside a `dwb`-launched
 * demo), so it stays invisible unless it's useful.
 */
export default function QuickConnect({ platform, onPick, refreshKey }: Props) {
  const [sources, setSources] = useState<ProvisionedSource[]>([]);
  const [open, setOpen] = useState(true);

  useEffect(() => {
    let cancelled = false;
    api
      .get("/api/dev/provisioned-sources")
      .then((r) => {
        if (cancelled) return;
        const all: ProvisionedSource[] = r.data?.sources ?? [];
        setSources(platform ? all.filter((s) => s.platform === platform) : all);
      })
      .catch(() => { if (!cancelled) setSources([]); });
    return () => { cancelled = true; };
  }, [platform, refreshKey]);

  if (sources.length === 0) return null;

  return (
    <div style={{
      border: "1px solid #bbf7d0", borderRadius: 8, backgroundColor: "#f0fdf4",
      padding: "8px 12px",
    }}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        style={{
          display: "flex", alignItems: "center", gap: 6, width: "100%",
          background: "transparent", border: "none", cursor: "pointer",
          padding: 0, fontSize: 12, fontWeight: 700, color: "#166534",
        }}
      >
        <span style={{ transform: open ? "rotate(90deg)" : "none", transition: "transform 0.15s" }}>▸</span>
        Quick connect
        <span style={{ fontWeight: 500, color: "#15803d" }}>
          — prefill from a launched sample DB ({sources.length})
        </span>
      </button>
      {open && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 8 }}>
          {sources.map((s) => (
            <button
              key={s.name}
              type="button"
              onClick={() => onPick(s)}
              title={`Fill the form with ${s.name} (${s.username}@${s.host}:${s.port}/${s.database})`}
              style={{
                textAlign: "left", padding: "6px 10px", borderRadius: 6,
                border: "1px solid #86efac", backgroundColor: "#fff",
                cursor: "pointer", fontSize: 12, color: "#14532d",
              }}
            >
              <strong>{s.name}</strong>
              <span style={{ color: "#15803d", textTransform: "uppercase", fontSize: 10, marginLeft: 6 }}>
                {s.platform}
              </span>
              <div style={{ color: "#64748b", fontSize: 11, marginTop: 2 }}>
                {s.platform === "s3"
                  ? `${s.host}:${s.port} · bucket: ${s.database} · key: ${s.username}`
                  : `${s.host}:${s.port} / ${s.database} · schema ${s.schema} · ${s.username}`}
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
