import { useEffect, useState } from "react";
import api from "../../api/client";
import FlowSankeyView from "./FlowSankeyView";
import type { FlowPayload } from "./flowSankeyTypes";

/**
 * Marketplace "Sankey view" tab. Self-fetches `GET /api/marketplace/flow`
 * (mirrors `ProductLineageView`'s own `useEffect` + `api.get`) and renders the
 * context-agnostic `FlowSankeyView` in the marketplace context.
 */

interface Props {
  /** Deep-link a product node to its marketplace detail (parent switches tab). */
  onOpenNode?: (uri: string) => void;
}

export default function FlowSankeyCatalogTab({ onOpenNode }: Props) {
  const [payload, setPayload] = useState<FlowPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .get("/api/marketplace/flow")
      .then((res) => { if (!cancelled) setPayload(res.data); })
      .catch(() => { if (!cancelled) setError("Failed to load the value-flow view."); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  return (
    <FlowSankeyView
      context="marketplace"
      payload={payload}
      loading={loading}
      error={error}
      onOpenNode={onOpenNode}
    />
  );
}
