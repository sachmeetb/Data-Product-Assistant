import { useEffect, useMemo, useState } from "react";

interface Props {
  status: "running" | "complete" | "failed" | "awaiting_review" | string;
  startedAt: string | null;
  completedAt: string | null;
  costUsd: number | null;
  eventCount: number;
  toolCounts: Record<string, number>;
  truncated?: boolean;
}

const MAX_CHIPS = 6;

function formatDuration(ms: number): string {
  if (ms < 0 || !Number.isFinite(ms)) return "—";
  const s = Math.floor(ms / 1000);
  const mm = Math.floor(s / 60);
  const ss = s % 60;
  const hh = Math.floor(mm / 60);
  if (hh > 0) {
    return `${hh}:${String(mm % 60).padStart(2, "0")}:${String(ss).padStart(2, "0")}`;
  }
  return `${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}`;
}

const STATUS_COLORS: Record<string, { bg: string; fg: string }> = {
  running: { bg: "#fef3c7", fg: "#92400e" },
  complete: { bg: "#dcfce7", fg: "#166534" },
  failed: { bg: "#fee2e2", fg: "#991b1b" },
  awaiting_review: { bg: "#ede9fe", fg: "#5b21b6" },
};

export default function StageOutputHeader({
  status,
  startedAt,
  completedAt,
  costUsd,
  eventCount,
  toolCounts,
  truncated,
}: Props) {
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    if (status !== "running") return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [status]);

  const elapsedMs = useMemo(() => {
    if (!startedAt) return 0;
    const start = new Date(startedAt).getTime();
    if (Number.isNaN(start)) return 0;
    const end = status === "running" ? now : completedAt ? new Date(completedAt).getTime() : now;
    return end - start;
  }, [startedAt, completedAt, status, now]);

  const toolEntries = useMemo(() => {
    const entries = Object.entries(toolCounts).sort((a, b) => b[1] - a[1]);
    const visible = entries.slice(0, MAX_CHIPS);
    const hidden = entries.length - visible.length;
    return { visible, hidden };
  }, [toolCounts]);

  const statusColor = STATUS_COLORS[status] ?? { bg: "#e2e8f0", fg: "#334155" };

  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        alignItems: "center",
        gap: 8,
        padding: "8px 10px",
        backgroundColor: "#f8fafc",
        border: "1px solid #e2e8f0",
        borderRadius: 6,
        marginBottom: 8,
        fontSize: 12,
      }}
    >
      <span
        style={{
          padding: "2px 10px",
          borderRadius: 10,
          fontWeight: 700,
          textTransform: "uppercase",
          fontSize: 11,
          backgroundColor: statusColor.bg,
          color: statusColor.fg,
        }}
      >
        {status}
      </span>
      {startedAt && (
        <Chip label={status === "running" ? "Elapsed" : "Duration"} value={formatDuration(elapsedMs)} />
      )}
      <Chip label="Events" value={String(eventCount)} />
      {costUsd != null && <Chip label="Cost" value={`$${costUsd.toFixed(4)}`} />}
      {truncated && (
        <span
          style={{
            padding: "2px 8px",
            borderRadius: 4,
            fontSize: 11,
            backgroundColor: "#fee2e2",
            color: "#991b1b",
            fontWeight: 600,
          }}
        >
          transcript truncated
        </span>
      )}
      {toolEntries.visible.length > 0 && (
        <>
          <span style={{ color: "#94a3b8", marginLeft: 4 }}>·</span>
          {toolEntries.visible.map(([name, count]) => (
            <span
              key={name}
              style={{
                padding: "2px 8px",
                borderRadius: 4,
                fontSize: 11,
                backgroundColor: "#e0f2fe",
                color: "#0369a1",
                fontWeight: 600,
              }}
            >
              {name} ×{count}
            </span>
          ))}
          {toolEntries.hidden > 0 && (
            <span style={{ fontSize: 11, color: "#64748b" }}>+{toolEntries.hidden} more</span>
          )}
        </>
      )}
    </div>
  );
}

function Chip({ label, value }: { label: string; value: string }) {
  return (
    <span
      style={{
        padding: "2px 8px",
        borderRadius: 4,
        fontSize: 11,
        backgroundColor: "#fff",
        border: "1px solid #e2e8f0",
        color: "#334155",
      }}
    >
      <span style={{ color: "#64748b", marginRight: 4 }}>{label}</span>
      <strong>{value}</strong>
    </span>
  );
}
