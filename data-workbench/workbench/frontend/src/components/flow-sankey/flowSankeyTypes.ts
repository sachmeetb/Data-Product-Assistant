/**
 * Shared types for the layered value-flow ("Sankey") view.
 *
 * Mirrors the backend `flow_payload.FlowBuilder` dict exactly (see
 * `workbench/backend/flow_payload.py`). The SAME payload/component serves both
 * the Marketplace (v1) and the Estate/Feasibility (fast-follow) contexts —
 * nothing here is marketplace-specific.
 *
 * `FlowNodeData` extends `Record<string, unknown>` so it satisfies React Flow's
 * `Node<T>` data constraint; `FlowLink` carries an index signature so it's
 * structurally assignable to `estateLayout.GraphEdge` and can be fed straight
 * into that module's `traverse` with no re-mapping.
 */

export interface FlowNode {
  id: string;
  label: string;
  /** One of the 5 column keys: source_system | source_aligned | aggregate | consumer | use_case. */
  column: string;
  kind?: string;
  /** Marketplace: lifecycle_state; Estate: feasibility tier. */
  status?: string;
  /** Source node = # products fed; product node = # direct downstream consumers. */
  count?: number;
  meta?: Record<string, unknown>;
  /** Synthesized ghost (empty-column filler or an estate "shopping-list" gap). Inert. */
  placeholder?: boolean;
}

export interface FlowColumn {
  key: string;
  label: string;
  nodes: FlowNode[];
}

export interface FlowLink {
  source: string;
  target: string;
  /** consumes | source_binding | supports | feasibility_match */
  kind: string;
  status?: string;
  weight?: number;
  /** Index signature → structurally a GraphEdge (reuse estateLayout.traverse). */
  [k: string]: unknown;
}

export interface FlowPayload {
  /** ALWAYS 5, fixed order (source_system, source_aligned, aggregate, consumer, use_case). */
  columns: FlowColumn[];
  links: FlowLink[];
}

export type FlowContext = "marketplace" | "estate";

/** Data carried on each React Flow node. */
export interface FlowNodeData extends Record<string, unknown> {
  id: string;
  label: string;
  column: string;
  kind?: string;
  status?: string;
  count?: number;
  meta?: Record<string, unknown>;
  placeholder?: boolean;
  context: FlowContext;
  /** Rendering state, injected by the view each render. */
  dimmed: boolean;
  focused: boolean;
  /** Reuse-heat 0..1 when the heat toggle is on and this is a product column; else undefined. */
  heat?: number;
  /** Called when the node's "Open ↗" affordance is clicked (product nodes only). */
  onOpen: (id: string) => void;
}
