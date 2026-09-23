// Renders the structured `discovery_view` payload for the data-discovery step.
//
// This is a faithful port of the "DiscoveryReport" component from the
// bp Data Agents Discovery prototype. It keeps the prototype's visual design
// (inline styles + the BP palette) while consuming the EXISTING backend
// `discovery_view` payload — no backend changes required.
//
// Sections (matching the prototype, top to bottom):
//   - Header:                "Data Discovery Results"
//   - USE CASE
//   - SUMMARY:               Gold / Silver / Bronze status cards
//   - SUMMARY BY DATA POINTS: data point -> result -> table(s) -> matched logic
//   - SUMMARY BY LAYER:      per-layer table cards with sample data + logic
//   - VISUAL DISCOVERY MAP:  vertical Gold -> Silver -> Bronze flow
//   - RESULT:                one-line verdict strip
//
// Clicking a table name opens a Knowledge-Catalog-style detail drawer,
// populated from the discovery data the backend returns.

import { useState } from 'react'

// ── bp brand palette (ported from the prototype) ─────────────────────────────
const BP = {
  green: '#A100FF',
  greenDark: '#7B00CC',
  greenDeep: '#2D0057',
  greenDeeper: '#1A0033',
  greenLight: '#F0E0FF',
  greenSoft: '#E5C5FF',
  greenLine: '#D4A0FF',
  yellow: '#FFC72C',
  amber: '#F2A516',
  text: '#0F1A14',
  textMuted: '#5C6B62',
  textSubtle: '#8A9A91',
  bg: '#F9F4FF',
  panel: '#FFFFFF',
  border: '#E8D5FF',
  warnBg: '#FFF6E5',
  warnLine: '#F2D89A',
}

const MONO = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'

const STATUS_PALETTE = {
  found:     { bg: BP.greenLight, line: BP.greenLine, head: BP.greenDark, lbl: 'Found' },
  not_found: { bg: '#FAEEEE', line: '#EBC1BE', head: '#9F2D26', lbl: 'Not found' },
  partial:   { bg: '#FFF8E7', line: '#F2D89A', head: '#9A6200', lbl: 'Partial' },
  pending:   { bg: '#F4F6F4', line: '#E1E6E2', head: '#5C6B62', lbl: 'Scanning…' },
}

const LAYER_TINT    = { gold: '#9A6A00', silver: '#4C5660', bronze: '#8A4A1C' }
const LAYER_BG_TINT = { gold: '#FFF6E2', silver: '#F1F3F5', bronze: '#FBEFE3' }
const LAYER_BORDER  = { gold: '#EBD9A8', silver: '#D3D8DD', bronze: '#E5C7A6' }
const LAYERS = ['gold', 'silver', 'bronze']

const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s)
const pal = (status) => STATUS_PALETTE[status] || STATUS_PALETTE.not_found

// ── Adapter: backend discovery_view -> prototype `d` shape ───────────────────
function adaptView(view) {
  const tablesByLayer = view.tables_by_layer || {}
  const layerSummary = view.layer_summary || {}

  // Per-layer status + synthesized message (backend doesn't ship a message).
  const layers = {}
  for (const layer of LAYERS) {
    const block = layerSummary[layer] || {}
    const count = block.table_count ?? (tablesByLayer[layer] || []).length
    const status = block.status || (count > 0 ? 'found' : 'not_found')
    let message
    if (status === 'found')        message = `${count} table${count === 1 ? '' : 's'} matched`
    else if (status === 'partial') message = `${count} reusable · more to design`
    else if (status === 'pending') message = 'Scanning next…'
    else                           message = 'No matching tables in this layer'
    layers[layer] = { status, tableCount: count, message }
  }

  // SUMMARY BY DATA POINTS
  const byDataPoint = (view.summary_by_data_points || []).map((r) => ({
    dataPoint: r.data_point,
    result: r.result,
    tables: r.tables || [],
    logic: r.matched_column_or_logic || '—',
  }))

  // Lookups for the table-detail drawer.
  const descByFull = {}
  for (const e of view.layers_plan || []) {
    for (const t of e.tables || []) {
      if (t.full_name) descByFull[t.full_name] = t.description || ''
    }
  }
  const tableIndex = {}   // full_name -> { card, layer, description }
  const shortToFull = {}  // short_name -> full_name

  // SUMMARY BY LAYER (every layer that has matched tables)
  const byLayer = {}
  for (const layer of LAYERS) {
    const cards = tablesByLayer[layer] || []
    cards.forEach((c) => {
      if (c.table_full_name) {
        tableIndex[c.table_full_name] = { card: c, layer, description: descByFull[c.table_full_name] || '' }
      }
      if (c.table_short_name) shortToFull[c.table_short_name] = c.table_full_name || c.table_short_name
    })
    if (!cards.length) continue
    byLayer[layer] = {
      heading: `${cap(layer)} — ${cards.length} table${cards.length === 1 ? '' : 's'}`,
      tables: cards.map((c) => ({
        tableId: c.table_full_name,
        tableName: c.table_short_name,
        rows: (c.rows || []).map((rw) => ({
          dataPoint: rw.data_point,
          sample: rw.sample_data_point_value || '—',
          logic: rw.matched_column_or_logic || '—',
          matchedSample: rw.sample_matched_value || '—',
        })),
      })),
    }
  }

  // VISUAL DISCOVERY MAP — keep the prototype's Gold→Silver→Bronze layout;
  // each layer shows column cards when it has matched tables, else a note.
  const visualMap = {}
  for (const layer of LAYERS) {
    const cards = tablesByLayer[layer] || []
    const status = layers[layer].status
    if (cards.length) {
      visualMap[layer] = {
        status,
        tableCount: cards.length,
        tables: cards.map((c) => ({
          name: c.table_short_name,
          columns: (c.rows || []).map((rw) => ({
            dataPoint: rw.data_point,
            colExpr: rw.matched_column_or_logic || '—',
          })),
        })),
      }
    } else {
      visualMap[layer] = {
        status,
        note: status === 'found'
          ? 'Matched in this layer.'
          : `No matching tables found in ${cap(layer)}.`,
      }
    }
  }

  return {
    useCase: view.use_case || '',
    layers,
    byDataPoint,
    byLayer,
    visualMap,
    result: view.result_text || view.headline || '',
    tableIndex,
    shortToFull,
  }
}

// ── Small building blocks ────────────────────────────────────────────────────
function SectionLabel({ children, style }) {
  return (
    <div style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: '0.06em', color: BP.textMuted, ...style }}>
      {children}
    </div>
  )
}

function SummaryCard({ layer, block }) {
  const p = pal(block.status)
  return (
    <div style={{
      background: 'white',
      border: `1px solid ${p.line}`,
      borderLeft: `4px solid ${LAYER_TINT[layer]}`,
      borderRadius: 8,
      padding: '12px 14px',
      flex: 1,
      minWidth: 0,
    }}>
      <div style={{ fontSize: 11.5, fontWeight: 700, color: LAYER_TINT[layer], letterSpacing: 0.3 }}>
        {cap(layer)}
      </div>
      <div style={{ fontSize: 13, fontWeight: 700, color: p.head, marginTop: 2 }}>{p.lbl}</div>
      <div style={{ fontSize: 11.5, color: BP.textMuted, marginTop: 6, lineHeight: 1.45 }}>
        {block.message}
      </div>
    </div>
  )
}

function TableChip({ name, onClick, clickable }) {
  const base = {
    textAlign: 'left',
    color: BP.greenDark,
    background: BP.greenLight,
    border: `1px solid ${BP.greenLine}`,
    borderRadius: 4,
    padding: '2px 6px',
    fontFamily: MONO,
    fontSize: 11,
    width: 'fit-content',
  }
  if (!clickable) return <div style={base}>{name}</div>
  return (
    <button type="button" onClick={onClick} style={{ ...base, cursor: 'pointer' }} title="View table details">
      {name}
    </button>
  )
}

// ── Visual discovery map: one layer band ─────────────────────────────────────
function VisualLayerBand({ layer, block }) {
  const tint = LAYER_TINT[layer]
  const head = pal(block.status).head
  const lbl = pal(block.status).lbl

  const labelBox = (
    <div style={{ background: LAYER_BG_TINT[layer], border: `1px solid ${LAYER_BORDER[layer]}`, borderRadius: 6, padding: '10px 12px' }}>
      <div style={{ fontSize: 11.5, fontWeight: 700, color: tint }}>{cap(layer)}</div>
      <div style={{ fontSize: 10.5, fontWeight: 700, color: head, marginTop: 4 }}>
        {lbl}{block.tableCount ? `: ${block.tableCount} table${block.tableCount === 1 ? '' : 's'}` : ''}
      </div>
    </div>
  )

  // Note-style band (no matched tables in this layer)
  if (!block.tables || !block.tables.length) {
    return (
      <div style={{ display: 'grid', gridTemplateColumns: '120px 1fr', gap: 10, alignItems: 'stretch' }}>
        {labelBox}
        <div style={{ background: 'white', border: `1px solid ${BP.border}`, borderRadius: 6, padding: '10px 12px', color: BP.textMuted, fontSize: 11.5, display: 'flex', alignItems: 'center' }}>
          {block.note}
        </div>
      </div>
    )
  }

  // Column-card band (matched tables)
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '120px 1fr', gap: 10, alignItems: 'stretch' }}>
      {labelBox}
      <div style={{ display: 'grid', gridTemplateColumns: `repeat(${block.tables.length || 1}, 1fr)`, gap: 8 }}>
        {block.tables.map((t, i) => (
          <div key={i} style={{ background: 'white', border: `1px solid ${BP.greenLine}`, borderRadius: 6, overflow: 'hidden' }}>
            <div style={{ background: BP.greenLight, padding: '6px 8px', borderBottom: `1px solid ${BP.greenLine}`, fontFamily: MONO, fontSize: 10.5, color: BP.greenDark, fontWeight: 600 }}>
              {t.name}
            </div>
            {t.columns.map((col, ci) => (
              <div key={ci} style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', padding: '5px 8px', borderTop: ci > 0 ? `1px solid ${BP.border}` : 'none', fontSize: 10.5 }}>
                <div style={{ color: BP.text, fontFamily: MONO }}>{col.dataPoint}</div>
                <div style={{ color: BP.textMuted, fontFamily: MONO }}>{col.colExpr}</div>
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  )
}

const Arrow = () => (
  <div style={{ textAlign: 'center', color: BP.textSubtle, fontSize: 14, padding: '4px 0' }}>↓</div>
)

// ── Table detail drawer (UCTablePanel port) ──────────────────────────────────
function TableDetailDrawer({ entry, onClose }) {
  const { card, layer, description } = entry
  const full = card.table_full_name || card.table_short_name || ''
  const parts = full.split('.')
  const catalog = parts.length >= 3 ? parts[0] : ''
  const schema = parts.length >= 3 ? parts[1] : parts.length === 2 ? parts[0] : ''
  const name = card.table_short_name || parts[parts.length - 1] || full
  const rows = card.rows || []

  return (
    <div
      onClick={onClose}
      style={{ position: 'fixed', inset: 0, background: 'rgba(15,26,20,0.35)', zIndex: 1000, display: 'flex', justifyContent: 'flex-end' }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{ width: 380, maxWidth: '92vw', height: '100%', background: BP.panel, borderLeft: `1px solid ${BP.border}`, boxShadow: '-8px 0 24px rgba(10,77,42,0.18)', display: 'flex', flexDirection: 'column' }}
      >
        {/* Header */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '0 12px', height: 56, flexShrink: 0, background: BP.greenDeep, borderBottom: `1px solid ${BP.greenDeeper}` }}>
          <button
            type="button"
            onClick={onClose}
            title="Close"
            style={{ width: 28, height: 28, borderRadius: 6, background: 'rgba(255,255,255,0.12)', color: 'white', cursor: 'pointer', border: 'none', fontSize: 15 }}
          >
            ←
          </button>
          <span style={{ color: BP.yellow, fontSize: 15 }}>▤</span>
          <div style={{ minWidth: 0 }}>
            <div style={{ color: 'white', fontSize: 11.5, fontWeight: 600, lineHeight: 1.2 }}>Knowledge Catalog</div>
            <div style={{ fontSize: 10.5, color: 'rgba(255,255,255,0.7)' }}>Table details</div>
          </div>
        </div>

        {/* Body */}
        <div style={{ overflowY: 'auto', flex: 1 }}>
          <div style={{ padding: '12px 16px', background: 'linear-gradient(180deg, #F4F8F5 0%, white 100%)', borderBottom: `1px solid ${BP.border}` }}>
            {(catalog || schema) && (
              <div style={{ fontSize: 10.5, fontWeight: 600, marginBottom: 4, color: BP.textMuted, fontFamily: MONO }}>
                {catalog} <span style={{ color: BP.textSubtle }}>/</span> {schema}
              </div>
            )}
            <div style={{ fontSize: 18, fontWeight: 700, display: 'flex', alignItems: 'center', gap: 8, color: BP.text, fontFamily: MONO }}>
              <span style={{ color: BP.amber }}>▤</span> {name}
            </div>
            <div style={{ fontSize: 10.5, marginTop: 6, display: 'inline-block', padding: '1px 8px', borderRadius: 999, fontWeight: 700, background: BP.greenLight, color: BP.greenDark, border: `1px solid ${BP.greenLine}` }}>
              {cap(layer)}
            </div>
            {description && (
              <div style={{ fontSize: 12, marginTop: 8, color: BP.text, lineHeight: 1.5 }}>{description}</div>
            )}
          </div>

          {/* Matched columns / logic */}
          <div style={{ padding: '12px 16px' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
              <SectionLabel>MATCHED COLUMNS / LOGIC</SectionLabel>
              <div style={{ fontSize: 10.5, fontWeight: 700, color: BP.greenDark }}>{rows.length}</div>
            </div>
            {rows.length ? (
              <div style={{ border: `1px solid ${BP.border}`, borderRadius: 6, overflow: 'hidden' }}>
                {rows.map((r, i) => (
                  <div key={i} style={{ padding: '8px 10px', borderBottom: i < rows.length - 1 ? '1px solid #EEF1EE' : 'none', background: i % 2 === 0 ? 'white' : '#FAFBFA' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span style={{ fontSize: 11.5, fontWeight: 600, color: BP.greenDeep, fontFamily: MONO }}>{r.data_point}</span>
                      <span style={{ marginLeft: 'auto', fontSize: 10.5, fontWeight: 600, padding: '1px 6px', borderRadius: 4, background: BP.greenLight, color: BP.greenDark, fontFamily: MONO }}>
                        {r.matched_column_or_logic || '—'}
                      </span>
                    </div>
                    {r.sample_matched_value && (
                      <div style={{ fontSize: 10.5, marginTop: 2, color: BP.textMuted, fontFamily: MONO }}>
                        sample: {r.sample_matched_value}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              <div style={{ fontSize: 11.5, color: BP.textMuted }}>No matched columns recorded for this table.</div>
            )}
          </div>

          {/* Footer */}
          <div style={{ padding: '12px 16px', borderTop: `1px solid ${BP.border}`, background: '#FAFBFA' }}>
            <div style={{ width: '100%', fontSize: 11.5, fontWeight: 600, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6, padding: '8px 12px', borderRadius: 6, background: 'white', color: BP.greenDark, border: `1px solid ${BP.greenLine}` }}>
              ▤ {full || name}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

// ── Domain Framework inline banner ───────────────────────────────────────────
function DomainFrameworkBanner({ framework }) {
  if (!framework?.name) return null

  const color = framework.color || '#6366f1'
  const hierarchy = framework.hierarchy || []
  const SPINE_MAX = 4
  const visibleSpine = hierarchy.slice(0, SPINE_MAX)
  const extraCount = Math.max(0, hierarchy.length - SPINE_MAX)

  return (
    <div style={{ marginTop: 14 }}>
      <SectionLabel>DOMAIN FRAMEWORK APPLIED</SectionLabel>
      <div style={{
        marginTop: 6,
        background: 'white',
        border: `1px solid ${BP.border}`,
        borderLeft: `4px solid ${color}`,
        borderRadius: 8,
        padding: '12px 14px',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 5 }}>
          <div style={{ width: 8, height: 8, borderRadius: '50%', background: color, flexShrink: 0 }} />
          <span style={{ fontSize: 12.5, fontWeight: 700, color: BP.text }}>{framework.display_name}</span>
        </div>

        <div style={{ fontSize: 11.5, color: BP.textMuted, marginBottom: 10, lineHeight: 1.45 }}>
          {framework.summary}
        </div>

        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
          {framework.entity_count && (
            <span style={{ fontSize: 10.5, fontWeight: 600, color: BP.text, background: BP.bg, border: `1px solid ${BP.border}`, borderRadius: 4, padding: '2px 8px' }}>
              {framework.entity_count} entities
            </span>
          )}
          {(framework.dimension_count || 0) > 0 && (
            <span style={{ fontSize: 10.5, color: BP.textMuted, background: BP.bg, border: `1px solid ${BP.border}`, borderRadius: 4, padding: '2px 8px' }}>
              {framework.dimension_count} dimensions
            </span>
          )}
          {(framework.event_count || 0) > 0 && (
            <span style={{ fontSize: 10.5, color: BP.textMuted, background: BP.bg, border: `1px solid ${BP.border}`, borderRadius: 4, padding: '2px 8px' }}>
              {framework.event_count} events
            </span>
          )}
          {(framework.aggregate_count || 0) > 0 && (
            <span style={{ fontSize: 10.5, color: BP.textMuted, background: BP.bg, border: `1px solid ${BP.border}`, borderRadius: 4, padding: '2px 8px' }}>
              {framework.aggregate_count} aggregates
            </span>
          )}
        </div>

        {framework.key_metrics?.length > 0 && (
          <div style={{ display: 'flex', gap: 5, alignItems: 'center', flexWrap: 'wrap', marginBottom: visibleSpine.length > 0 ? 10 : 0 }}>
            <span style={{ fontSize: 10, fontWeight: 700, letterSpacing: '0.06em', color: BP.textSubtle }}>KEY METRICS</span>
            {framework.key_metrics.map(m => (
              <span key={m} style={{ fontSize: 10.5, fontWeight: 700, color: color, background: 'white', border: `1px solid ${BP.border}`, borderRadius: 3, padding: '1px 6px', fontFamily: MONO }}>
                {m}
              </span>
            ))}
          </div>
        )}

        {visibleSpine.length > 0 && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 4, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 10, fontWeight: 700, letterSpacing: '0.06em', color: BP.textSubtle, marginRight: 2 }}>SPINE</span>
            {visibleSpine.map((h, i) => (
              <span key={h} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <span style={{ fontSize: 10.5, fontFamily: MONO, color: BP.greenDark, background: BP.greenLight, border: `1px solid ${BP.greenLine}`, borderRadius: 3, padding: '1px 5px' }}>{h}</span>
                {(i < visibleSpine.length - 1 || extraCount > 0) && (
                  <span style={{ color: BP.textSubtle, fontSize: 10 }}>→</span>
                )}
              </span>
            ))}
            {extraCount > 0 && (
              <span style={{ fontSize: 10.5, color: BP.textSubtle }}>+{extraCount} more</span>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// ── Main ─────────────────────────────────────────────────────────────────────
export default function DiscoveryResultCard({ view }) {
  const [openTableId, setOpenTableId] = useState(null)
  if (!view) return null

  const d = adaptView(view)

  const resolveTable = (id) => {
    if (!id) return null
    if (d.tableIndex[id]) return d.tableIndex[id]
    const full = d.shortToFull[id]
    if (full && d.tableIndex[full]) return d.tableIndex[full]
    return null
  }
  const openEntry = resolveTable(openTableId)

  const hasByDataPoint = d.byDataPoint.length > 0
  const byLayerKeys = LAYERS.filter((l) => d.byLayer[l])

  return (
    <div style={{ background: 'white', border: `1px solid ${BP.border}`, borderRadius: 10, padding: 18, maxWidth: 760, color: BP.text }}>
      <div style={{ fontSize: 13, fontWeight: 700, color: BP.text }}>Data Discovery Results</div>

      {/* USE CASE */}
      <div style={{ marginTop: 14 }}>
        <SectionLabel>USE CASE</SectionLabel>
        <div style={{ fontSize: 12.5, color: BP.text, marginTop: 4, paddingBottom: 8, borderBottom: `1px solid ${BP.border}` }}>
          {d.useCase || '—'}
        </div>
      </div>

      {/* DOMAIN FRAMEWORK BANNER — inline when backend matched a known domain */}
      <DomainFrameworkBanner framework={view.domain_framework} />

      {/* SUMMARY */}
      <div style={{ marginTop: 14 }}>
        <SectionLabel style={{ marginBottom: 8 }}>SUMMARY</SectionLabel>
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
          {LAYERS.map((layer) => (
            <SummaryCard key={layer} layer={layer} block={d.layers[layer]} />
          ))}
        </div>
      </div>

      {/* SUMMARY BY DATA POINTS */}
      {hasByDataPoint && (
        <div style={{ marginTop: 18 }}>
          <SectionLabel style={{ marginBottom: 8 }}>SUMMARY BY DATA POINTS</SectionLabel>
          <div style={{ border: `1px solid ${BP.border}`, borderRadius: 8, overflow: 'hidden' }}>
            <div style={{ display: 'grid', gridTemplateColumns: '1.2fr 1.2fr 2fr 2fr', background: BP.greenLight, padding: '8px 10px', borderBottom: `1px solid ${BP.greenLine}` }}>
              {['DATA POINT', 'RESULT', 'TABLE', 'MATCHED COLUMN / LOGIC'].map((h) => (
                <div key={h} style={{ fontSize: 10.5, fontWeight: 700, color: BP.greenDark }}>{h}</div>
              ))}
            </div>
            {d.byDataPoint.map((r, i) => (
              <div key={i} style={{ display: 'grid', gridTemplateColumns: '1.2fr 1.2fr 2fr 2fr', padding: '8px 10px', borderBottom: i < d.byDataPoint.length - 1 ? `1px solid ${BP.border}` : 'none', fontSize: 11.5, alignItems: 'start' }}>
                <div style={{ color: BP.text, fontFamily: MONO }}>{r.dataPoint}</div>
                <div style={{ color: BP.greenDark }}>{r.result}</div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
                  {r.tables.length
                    ? r.tables.map((t) => (
                        <TableChip key={t} name={t} clickable={!!resolveTable(t)} onClick={() => setOpenTableId(t)} />
                      ))
                    : <span style={{ color: BP.textSubtle }}>—</span>}
                </div>
                <div style={{ color: BP.text }}>{r.logic}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* SUMMARY BY LAYER */}
      {byLayerKeys.length > 0 && (
        <div style={{ marginTop: 18 }}>
          <SectionLabel style={{ marginBottom: 8 }}>SUMMARY BY LAYER</SectionLabel>
          {byLayerKeys.map((layer) => {
            const lb = d.byLayer[layer]
            return (
              <div key={layer} style={{ marginBottom: 12 }}>
                <div style={{ fontSize: 12.5, fontWeight: 700, color: BP.text, marginBottom: 8 }}>{lb.heading}</div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                  {lb.tables.map((t, ti) => (
                    <div key={ti} style={{ border: `1px solid ${BP.border}`, borderRadius: 8, overflow: 'hidden' }}>
                      <div style={{ background: LAYER_BG_TINT[layer], padding: '8px 10px', borderBottom: `1px solid ${BP.border}`, fontSize: 11.5, color: BP.text }}>
                        <span style={{ color: BP.textMuted }}>Table: </span>
                        <TableChip
                          name={t.tableName}
                          clickable={!!resolveTable(t.tableId)}
                          onClick={() => setOpenTableId(t.tableId)}
                        />
                      </div>
                      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1.5fr 1.4fr 1.8fr', background: BP.greenLight, padding: '6px 10px', borderBottom: `1px solid ${BP.greenLine}`, fontSize: 10.5 }}>
                        {['DATA POINT', 'SAMPLE FOR DATA POINT', 'MATCHED COLUMN / LOGIC', 'SAMPLE DATA FROM MATCHED COLUMN/LOGIC'].map((h) => (
                          <div key={h} style={{ fontWeight: 700, color: BP.greenDark }}>{h}</div>
                        ))}
                      </div>
                      {t.rows.map((r, ri) => (
                        <div key={ri} style={{ display: 'grid', gridTemplateColumns: '1fr 1.5fr 1.4fr 1.8fr', padding: '7px 10px', borderBottom: ri < t.rows.length - 1 ? `1px solid ${BP.border}` : 'none', fontSize: 11, alignItems: 'start' }}>
                          <div style={{ color: BP.text, fontFamily: MONO }}>{r.dataPoint}</div>
                          <div style={{ color: BP.text }}>{r.sample}</div>
                          <div style={{ color: BP.text, fontFamily: MONO }}>{r.logic}</div>
                          <div style={{ color: BP.textMuted, fontFamily: MONO }}>{r.matchedSample}</div>
                        </div>
                      ))}
                    </div>
                  ))}
                </div>
              </div>
            )
          })}
        </div>
      )}

      {/* VISUAL DISCOVERY MAP */}
      <div style={{ marginTop: 18 }}>
        <SectionLabel style={{ marginBottom: 8 }}>VISUAL DISCOVERY MAP</SectionLabel>
        <div style={{ border: `1px solid ${BP.border}`, borderRadius: 8, padding: 14, background: '#FAFBFC' }}>
          <div style={{ background: 'white', border: `1px solid ${BP.border}`, borderRadius: 6, padding: '8px 10px' }}>
            <SectionLabel>USE CASE</SectionLabel>
            <div style={{ fontSize: 11.5, color: BP.text, marginTop: 2 }}>{d.useCase || '—'}</div>
          </div>
          <Arrow />
          <VisualLayerBand layer="gold" block={d.visualMap.gold} />
          <Arrow />
          <VisualLayerBand layer="silver" block={d.visualMap.silver} />
          <Arrow />
          <VisualLayerBand layer="bronze" block={d.visualMap.bronze} />
        </div>
      </div>

      {/* RESULT */}
      {d.result && (
        <div style={{ marginTop: 18 }}>
          <SectionLabel style={{ marginBottom: 6 }}>RESULT</SectionLabel>
          <div style={{ background: BP.greenLight, border: `1px solid ${BP.greenLine}`, borderRadius: 6, padding: '10px 12px', color: BP.greenDark, fontSize: 12 }}>
            {d.result}
          </div>
        </div>
      )}

      {openEntry && <TableDetailDrawer entry={openEntry} onClose={() => setOpenTableId(null)} />}
    </div>
  )
}
