import type { StageExecutionSummary } from "../types";

interface Props {
  executions: StageExecutionSummary[];
  liveRunIds: Set<string>;
  selectedExecutionId: number | null;
  liveSelected: boolean;
  onSelectLive: () => void;
  onSelectExecution: (id: number) => void;
}

function relativeTime(iso: string | null): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const diff = Math.max(0, Date.now() - then);
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}

export default function RunSelector({
  executions,
  liveRunIds,
  selectedExecutionId,
  liveSelected,
  onSelectLive,
  onSelectExecution,
}: Props) {
  const hasLive = liveRunIds.size > 0;
  // Hide when there is no history and no live run (or just one of either)
  const totalOptions = executions.length + (hasLive ? 1 : 0);
  if (totalOptions <= 1) return null;

  const handleChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    const v = e.target.value;
    if (v === "live") onSelectLive();
    else onSelectExecution(parseInt(v, 10));
  };

  const value = liveSelected ? "live" : selectedExecutionId != null ? String(selectedExecutionId) : "";

  const total = executions.length;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
      <label style={{ fontSize: 12, color: "#64748b", fontWeight: 600 }}>Run:</label>
      <select
        value={value}
        onChange={handleChange}
        style={{
          padding: "4px 8px",
          fontSize: 12,
          borderRadius: 4,
          border: "1px solid #cbd5e1",
          backgroundColor: "#fff",
          color: "#1e293b",
          cursor: "pointer",
        }}
      >
        {hasLive && (
          <option value="live">LIVE · running now</option>
        )}
        {executions.map((ex, i) => {
          const runNo = total - i;
          const cost = ex.cost_usd != null ? ` · $${ex.cost_usd.toFixed(4)}` : "";
          const rel = ex.started_at ? ` · ${relativeTime(ex.started_at)}` : "";
          const statusTag = ex.status === "failed" ? " · failed" : "";
          return (
            <option key={ex.id} value={ex.id}>
              Run {runNo}{rel}{cost} · {ex.event_count} events{statusTag}
            </option>
          );
        })}
      </select>
    </div>
  );
}
