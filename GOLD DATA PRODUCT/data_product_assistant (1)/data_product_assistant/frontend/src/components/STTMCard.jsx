/**
 * Renders the staged DDI STTM cards (Gold STTM Generator and Silver
 * Transformation Agent). The two cards share the same overall layout —
 * `variant` controls a colour accent only.
 *
 * For the silver variant, the long narrative paragraph is auto-split into
 * bullets per sentence (and per numbered marker like "(1)", "(2)") for
 * readability, and a Mermaid lineage diagram (Bronze → Silver → Gold) is
 * rendered below the STTM table.
 */
import MermaidBlock from './MermaidBlock'

function splitNarrativeIntoBullets(text) {
  if (!text || typeof text !== 'string') return []
  const trimmed = text.trim()
  if (!trimmed) return []

  // Find the index of the first numbered marker like "(1)" or "1." that starts
  // a list inside the paragraph. Everything before it is the "intro" sentences;
  // the numbered items become their own bullets.
  const numberedRe = /(?:\([0-9]+\)|(?:^|\s)[0-9]+\.\s)/g
  const matches = [...trimmed.matchAll(numberedRe)]

  const sentenceSplit = (s) =>
    s.split(/(?<=[.;])\s+(?=[A-Z(])/).map(p => p.trim()).filter(Boolean)

  if (matches.length < 2) {
    // No numbered list — just sentence-split the whole paragraph
    return sentenceSplit(trimmed)
  }

  const firstIdx = matches[0].index
  const intro = trimmed.slice(0, firstIdx).trim().replace(/[:;,]\s*$/, '')
  const listPart = trimmed.slice(firstIdx)

  // Split listPart at every numbered marker
  const items = listPart
    .split(/\([0-9]+\)|(?:^|\s)[0-9]+\.\s/)
    .map(s => s.trim().replace(/^[;,]\s*/, '').replace(/[;]\s*$/, ''))
    .filter(Boolean)

  const introBullets = intro ? sentenceSplit(intro) : []
  return [...introBullets, ...items]
}

function sanitiseId(s) {
  return String(s || '').replace(/[^a-zA-Z0-9_]/g, '_')
}

function stripCatalogPrefix(col) {
  if (!col) return col
  // Remove bp_source. / bp_consumption. / bp_aggregated. catalog prefixes from display
  return String(col).replace(/^bp_[a-z]+\./i, '')
}

function shortName(fqn) {
  if (!fqn) return ''
  const parts = String(fqn).split('.')
  return parts[parts.length - 1]
}

function tableOfColumn(fqColumn) {
  // "bp_source.digital.raw_user_device_visit_events.visit_id"
  //   → "bp_source.digital.raw_user_device_visit_events"
  if (!fqColumn) return ''
  const parts = String(fqColumn).split('.')
  if (parts.length < 2) return ''
  return parts.slice(0, -1).join('.')
}

function columnOf(fqColumn) {
  if (!fqColumn) return ''
  const parts = String(fqColumn).split('.')
  return parts[parts.length - 1]
}

// Map a transform label to a SQL-ish type string for the ER diagram column rows.
function typeFromTransform(transform) {
  if (!transform) return 'string'
  const t = String(transform).toUpperCase()
  if (t.includes('CAST_TO_BOOLEAN')) return 'boolean'
  if (t.includes('CAST_TO_INTEGER')) return 'int'
  if (t.includes('CAST_TO_DOUBLE'))  return 'double'
  if (t.includes('DATE_TRUNC_DAY') || t.includes('CAST_TO_DATE')) return 'date'
  if (t.includes('CAST_TO_TIMESTAMP') || t.includes('TIMESTAMP')) return 'timestamp'
  if (t.includes('COUNT_AGGREGATE')) return 'long'
  if (t.includes('SUM_AGGREGATE'))   return 'double'
  return 'string'
}

/**
 * Build a column-level ER diagram from the STTM mappings. Each unique source
 * table (Bronze) becomes one ER entity with its source columns; each unique
 * target table (Silver) becomes one ER entity with its target columns; gold
 * targets (when supplied) become trailing entities. Arrows go Bronze → Silver
 * → Gold so the user can read the data flow left-to-right.
 */
function buildLineageMermaid({ silver_tables = [], gold_targets = [], mappings = [] }) {
  if (!mappings || mappings.length === 0) {
    return ''
  }

  // Group columns per table from the mapping rows
  const bronzeByTable = new Map()  // table → Map<colName, type>
  const silverByTable = new Map()  // table → Map<colName, {type, isFromBronze}>

  for (const m of mappings) {
    const srcTable = tableOfColumn(m.source_column)
    const srcCol   = columnOf(m.source_column)
    const tgtTable = m.target_table || tableOfColumn(m.target_column)
    const tgtCol   = columnOf(m.target_column)
    const type     = typeFromTransform(m.transform)

    if (srcTable && srcCol) {
      if (!bronzeByTable.has(srcTable)) bronzeByTable.set(srcTable, new Map())
      // Bronze columns are typed as strings from the raw source; we don't infer Bronze types here.
      bronzeByTable.get(srcTable).set(srcCol, 'string')
    }
    if (tgtTable && tgtCol) {
      if (!silverByTable.has(tgtTable)) silverByTable.set(tgtTable, new Map())
      silverByTable.get(tgtTable).set(tgtCol, type)
    }
  }

  // Make sure silver_tables passed from the view are represented even if mappings
  // are empty for them (rare but defensive).
  for (const st of (silver_tables || [])) {
    const key = shortName(st)
    if (!silverByTable.has(key)) silverByTable.set(key, new Map())
  }

  const goldTables = (gold_targets || []).filter(Boolean)

  const lines = ['erDiagram']

  // Bronze entities
  for (const [table, cols] of bronzeByTable.entries()) {
    const id = sanitiseId(shortName(table))
    lines.push(`    ${id} {`)
    for (const [colName, colType] of cols.entries()) {
      lines.push(`        ${colType} ${sanitiseId(colName)}`)
    }
    lines.push('    }')
  }

  // Silver entities
  for (const [table, cols] of silverByTable.entries()) {
    const id = sanitiseId(shortName(table))
    lines.push(`    ${id} {`)
    for (const [colName, colType] of cols.entries()) {
      lines.push(`        ${colType} ${sanitiseId(colName)}`)
    }
    lines.push('    }')
  }

  // Gold entities (no column-level info available — emit a placeholder column)
  for (const gt of goldTables) {
    const id = sanitiseId(shortName(gt))
    lines.push(`    ${id} {`)
    lines.push(`        string id`)
    lines.push('    }')
  }

  // Relationships — Bronze → Silver for every (bronze, silver) pair seen in the mappings
  const bronzeToSilverPairs = new Set()
  for (const m of mappings) {
    const srcTable = shortName(tableOfColumn(m.source_column))
    const tgtTable = shortName(m.target_table || tableOfColumn(m.target_column))
    if (srcTable && tgtTable) {
      bronzeToSilverPairs.add(`${srcTable}::${tgtTable}`)
    }
  }
  for (const pair of bronzeToSilverPairs) {
    const [b, s] = pair.split('::')
    lines.push(`    ${sanitiseId(b)} ||--o{ ${sanitiseId(s)} : "conforms to"`)
  }

  // Silver → Gold for every silver/gold pair (no per-row info; assume each silver feeds every gold).
  for (const [silverTable] of silverByTable.entries()) {
    for (const gt of goldTables) {
      lines.push(`    ${sanitiseId(shortName(silverTable))} ||--o{ ${sanitiseId(shortName(gt))} : "feeds"`)
    }
  }

  return lines.join('\n')
}

export default function STTMCard({ view, variant = 'gold' }) {
  if (!view) return null
  const {
    title, step_label, summary, narrative,
    silver_sources, gold_targets, derived,
    lineage_summary, silver_tables,
    header, mappings = [], mapping_count,
  } = view

  const isSilver = variant === 'silver'
  const narrativeText = summary || narrative
  const narrativeBullets = isSilver ? splitNarrativeIntoBullets(narrativeText) : []
  const lineageChart = isSilver
    ? buildLineageMermaid({ silver_tables, gold_targets, mappings })
    : ''

  // Domain framework — use backend-injected value when present; otherwise detect from mappings/tables
  const CAMPAIGN_FRAMEWORK = {
    name: 'campaign',
    display_name: 'Digital Marketing & Ad-Tech',
    color: '#6366f1',
    summary: 'Canonical silver spine for campaign data: impressions → clicks → conversions → performance. Anchored to IAB OpenRTB (ad-tech vocabulary), Google CM / Meta Ads (metric structure), and Adobe XDM ExperienceEvent (journey event design).',
    entity_count: 11,
    standards: ['IAB_OpenRTB', 'Google_CM', 'Meta_Ads', 'Adobe_XDM_ExperienceEvent'],
    dimensions: ['slv_campaign', 'slv_ad_group', 'slv_ad', 'slv_creative', 'slv_channel', 'slv_placement', 'slv_audience'],
    events: ['slv_impression_event', 'slv_click_event', 'slv_conversion_event'],
    aggregates: ['slv_campaign_performance_daily'],
    derived_metrics: [
      { name: 'CTR',  formula: 'clicks / impressions', description: 'Click-through rate' },
      { name: 'CPC',  formula: 'spend / clicks',        description: 'Cost per click' },
      { name: 'CPA',  formula: 'spend / conversions',   description: 'Cost per acquisition' },
      { name: 'ROAS', formula: 'revenue / spend',       description: 'Return on ad spend' },
    ],
    channels: ['GOOGLE', 'META', 'DV360', 'EMAIL', 'PROGRAMMATIC'],
    entities: [
      { name: 'slv_campaign', type: 'dimension', grain: 'one row per campaign', columns: [
        { name: 'campaign_id',   type: 'STRING', pk: true,  fk: false },
        { name: 'campaign_name', type: 'STRING', pk: false, fk: false },
        { name: 'objective',     type: 'STRING', pk: false, fk: false },
        { name: 'start_date',    type: 'DATE',   pk: false, fk: false },
        { name: 'end_date',      type: 'DATE',   pk: false, fk: false },
        { name: 'budget_amount', type: 'DOUBLE', pk: false, fk: false },
        { name: 'business_unit', type: 'STRING', pk: false, fk: false },
      ]},
      { name: 'slv_ad_group', type: 'dimension', grain: 'one row per ad group / line item', columns: [
        { name: 'ad_group_id',   type: 'STRING', pk: true,  fk: false },
        { name: 'ad_group_name', type: 'STRING', pk: false, fk: false },
        { name: 'campaign_id',   type: 'STRING', pk: false, fk: true, fk_ref: 'slv_campaign' },
      ]},
      { name: 'slv_ad', type: 'dimension', grain: 'one row per ad', columns: [
        { name: 'ad_id',       type: 'STRING', pk: true,  fk: false },
        { name: 'ad_name',     type: 'STRING', pk: false, fk: false },
        { name: 'ad_group_id', type: 'STRING', pk: false, fk: true, fk_ref: 'slv_ad_group' },
        { name: 'campaign_id', type: 'STRING', pk: false, fk: true, fk_ref: 'slv_campaign' },
      ]},
      { name: 'slv_creative', type: 'dimension', grain: 'one row per creative asset', columns: [
        { name: 'creative_id',   type: 'STRING', pk: true,  fk: false },
        { name: 'creative_name', type: 'STRING', pk: false, fk: false },
        { name: 'creative_type', type: 'STRING', pk: false, fk: false },
        { name: 'ad_id',         type: 'STRING', pk: false, fk: true, fk_ref: 'slv_ad' },
      ]},
      { name: 'slv_channel', type: 'dimension', grain: 'one row per delivery channel', columns: [
        { name: 'channel_id',   type: 'STRING', pk: true,  fk: false },
        { name: 'channel_name', type: 'STRING', pk: false, fk: false },
        { name: 'channel_type', type: 'STRING', pk: false, fk: false },
      ]},
      { name: 'slv_placement', type: 'dimension', grain: 'one row per placement', columns: [
        { name: 'placement_id',   type: 'STRING', pk: true,  fk: false },
        { name: 'placement_name', type: 'STRING', pk: false, fk: false },
        { name: 'channel_id',     type: 'STRING', pk: false, fk: true, fk_ref: 'slv_channel' },
        { name: 'site_domain',    type: 'STRING', pk: false, fk: false },
      ]},
      { name: 'slv_audience', type: 'dimension', grain: 'one row per audience segment', columns: [
        { name: 'audience_id',   type: 'STRING', pk: true,  fk: false },
        { name: 'audience_name', type: 'STRING', pk: false, fk: false },
        { name: 'audience_type', type: 'STRING', pk: false, fk: false },
        { name: 'campaign_id',   type: 'STRING', pk: false, fk: true, fk_ref: 'slv_campaign' },
      ]},
      { name: 'slv_impression_event', type: 'event', grain: 'one row per ad view', columns: [
        { name: 'impression_id', type: 'STRING',    pk: true,  fk: false },
        { name: 'campaign_id',   type: 'STRING',    pk: false, fk: true, fk_ref: 'slv_campaign' },
        { name: 'ad_id',         type: 'STRING',    pk: false, fk: true, fk_ref: 'slv_ad' },
        { name: 'creative_id',   type: 'STRING',    pk: false, fk: true, fk_ref: 'slv_creative' },
        { name: 'channel',       type: 'STRING',    pk: false, fk: false },
        { name: 'user_id',       type: 'STRING',    pk: false, fk: false },
        { name: 'anonymous_id',  type: 'STRING',    pk: false, fk: false },
        { name: 'timestamp',     type: 'TIMESTAMP', pk: false, fk: false },
        { name: 'device_type',   type: 'STRING',    pk: false, fk: false },
        { name: 'geo',           type: 'STRING',    pk: false, fk: false },
      ]},
      { name: 'slv_click_event', type: 'event', grain: 'one row per click', columns: [
        { name: 'click_id',         type: 'STRING',    pk: true,  fk: false },
        { name: 'impression_id',    type: 'STRING',    pk: false, fk: true, fk_ref: 'slv_impression_event' },
        { name: 'campaign_id',      type: 'STRING',    pk: false, fk: true, fk_ref: 'slv_campaign' },
        { name: 'ad_id',            type: 'STRING',    pk: false, fk: true, fk_ref: 'slv_ad' },
        { name: 'timestamp',        type: 'TIMESTAMP', pk: false, fk: false },
        { name: 'landing_page_url', type: 'STRING',    pk: false, fk: false },
        { name: 'cost',             type: 'DOUBLE',    pk: false, fk: false },
      ]},
      { name: 'slv_conversion_event', type: 'event', grain: 'one row per conversion', columns: [
        { name: 'conversion_id',    type: 'STRING',    pk: true,  fk: false },
        { name: 'click_id',         type: 'STRING',    pk: false, fk: true, fk_ref: 'slv_click_event' },
        { name: 'campaign_id',      type: 'STRING',    pk: false, fk: true, fk_ref: 'slv_campaign' },
        { name: 'conversion_type',  type: 'STRING',    pk: false, fk: false },
        { name: 'conversion_value', type: 'DOUBLE',    pk: false, fk: false },
        { name: 'timestamp',        type: 'TIMESTAMP', pk: false, fk: false },
      ]},
      { name: 'slv_campaign_performance_daily', type: 'aggregate', grain: 'one row per campaign + channel + day', columns: [
        { name: 'date',        type: 'DATE',   pk: true,  fk: false },
        { name: 'campaign_id', type: 'STRING', pk: true,  fk: true, fk_ref: 'slv_campaign' },
        { name: 'channel',     type: 'STRING', pk: true,  fk: false },
        { name: 'impressions', type: 'BIGINT', pk: false, fk: false },
        { name: 'clicks',      type: 'BIGINT', pk: false, fk: false },
        { name: 'conversions', type: 'BIGINT', pk: false, fk: false },
        { name: 'spend',       type: 'DOUBLE', pk: false, fk: false },
        { name: 'revenue',     type: 'DOUBLE', pk: false, fk: false },
      ]},
    ],
  }

  function detectFramework() {
    if (!isSilver) return null
    if (view.domain_framework) return view.domain_framework
    const hasCampaign = (
      (mappings || []).some(m => /campaign/i.test(m.target_table || '') || /campaign/i.test(m.target_column || '')) ||
      (silver_tables || []).some(t => /campaign/i.test(String(t))) ||
      /campaign/i.test(narrative || '') ||
      /campaign/i.test(summary || '')
    )
    return hasCampaign ? CAMPAIGN_FRAMEWORK : null
  }

  const domainFramework = detectFramework()
  const dfColor = domainFramework?.color || '#6366f1'

  return (
    <div className={`sttm-card sttm-card-${variant}`}>
      <div className="sttm-card-header">
        <div className="sttm-card-avatar">{variant === 'silver' ? 'ST' : 'GS'}</div>
        <div className="sttm-card-titles">
          <div className="sttm-card-title">{title}</div>
          {step_label && <div className="sttm-card-step">{step_label}</div>}
        </div>
      </div>

      {domainFramework && (
        <div style={{
          border: '1px solid #e5e7eb',
          borderLeft: `4px solid ${dfColor}`,
          borderRadius: 6,
          margin: '0 0 10px',
          overflow: 'hidden',
          fontSize: 11,
        }}>
          {/* ── Header ── */}
          <div style={{ padding: '10px 14px 9px', background: 'white', borderBottom: '1px solid #f0f0f0' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 7, marginBottom: 5 }}>
              <div style={{ width: 8, height: 8, borderRadius: '50%', background: dfColor, flexShrink: 0 }} />
              <span style={{ fontSize: 11.5, fontWeight: 700, color: '#111827' }}>
                {domainFramework.display_name} — Framework Spine Applied
              </span>
              {domainFramework.entity_count && (
                <span style={{ marginLeft: 'auto', fontSize: 10, fontWeight: 600, color: dfColor, background: `${dfColor}18`, border: `1px solid ${dfColor}50`, borderRadius: 3, padding: '1px 7px', whiteSpace: 'nowrap' }}>
                  {domainFramework.entity_count} entities
                </span>
              )}
            </div>
            <div style={{ fontSize: 11, color: '#6b7280', lineHeight: 1.5 }}>
              {domainFramework.summary}
            </div>
          </div>

          {/* ── Entity Hierarchy ── */}
          {(domainFramework.dimensions || domainFramework.events || domainFramework.aggregates) && (
            <div style={{ padding: '8px 14px 10px', background: '#fafafa', borderBottom: '1px solid #f0f0f0' }}>
              <div style={{ fontSize: 9.5, fontWeight: 700, color: '#9ca3af', letterSpacing: '0.08em', marginBottom: 7 }}>ENTITY HIERARCHY</div>
              <div style={{ display: 'grid', gridTemplateColumns: '2fr 2fr 3fr', gap: 10 }}>
                <div>
                  <div style={{ fontSize: 9.5, fontWeight: 700, color: '#374151', marginBottom: 4, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                    Dimensions ({domainFramework.dimensions?.length ?? 0})
                  </div>
                  {(domainFramework.dimensions || []).map(e => (
                    <div key={e} style={{ fontSize: 10, color: '#4b5563', fontFamily: '"ui-monospace","SFMono-Regular",monospace', lineHeight: 1.7 }}>{e}</div>
                  ))}
                </div>
                <div>
                  <div style={{ fontSize: 9.5, fontWeight: 700, color: '#374151', marginBottom: 4, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                    Events ({domainFramework.events?.length ?? 0})
                  </div>
                  {(domainFramework.events || []).map(e => (
                    <div key={e} style={{ fontSize: 10, color: '#4b5563', fontFamily: '"ui-monospace","SFMono-Regular",monospace', lineHeight: 1.7 }}>{e}</div>
                  ))}
                </div>
                <div>
                  <div style={{ fontSize: 9.5, fontWeight: 700, color: '#374151', marginBottom: 4, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                    Aggregates ({domainFramework.aggregates?.length ?? 0})
                  </div>
                  {(domainFramework.aggregates || []).map(e => (
                    <div key={e} style={{ fontSize: 10, color: '#4b5563', fontFamily: '"ui-monospace","SFMono-Regular",monospace', lineHeight: 1.7 }}>{e}</div>
                  ))}
                </div>
              </div>
            </div>
          )}

          {/* ── Derived Metrics ── */}
          {domainFramework.derived_metrics?.length > 0 && (
            <div style={{ padding: '8px 14px 10px', background: 'white', borderBottom: '1px solid #f0f0f0' }}>
              <div style={{ fontSize: 9.5, fontWeight: 700, color: '#9ca3af', letterSpacing: '0.08em', marginBottom: 7 }}>DERIVED METRICS</div>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                {domainFramework.derived_metrics.map(m => (
                  <div key={m.name} style={{ background: '#fafafa', border: '1px solid #e5e7eb', borderRadius: 4, padding: '5px 9px', minWidth: 115 }}>
                    <div style={{ fontSize: 11, fontWeight: 700, color: dfColor, fontFamily: '"ui-monospace","SFMono-Regular",monospace', marginBottom: 2 }}>{m.name}</div>
                    <div style={{ fontSize: 10, color: '#374151', fontFamily: '"ui-monospace","SFMono-Regular",monospace', marginBottom: 1 }}>{m.formula}</div>
                    <div style={{ fontSize: 9.5, color: '#9ca3af' }}>{m.description}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* ── Standards + Channels ── */}
          <div style={{ padding: '8px 14px 10px', background: '#fafafa', borderBottom: '1px solid #f0f0f0', display: 'flex', gap: 20, flexWrap: 'wrap' }}>
            {domainFramework.standards?.length > 0 && (
              <div>
                <div style={{ fontSize: 9.5, fontWeight: 700, color: '#9ca3af', letterSpacing: '0.08em', marginBottom: 5 }}>STANDARDS</div>
                <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                  {domainFramework.standards.map(s => (
                    <span key={s} style={{ fontSize: 9.5, color: '#374151', background: 'white', border: '1px solid #d1d5db', borderRadius: 3, padding: '1px 6px' }}>{s}</span>
                  ))}
                </div>
              </div>
            )}
            {domainFramework.channels?.length > 0 && (
              <div>
                <div style={{ fontSize: 9.5, fontWeight: 700, color: '#9ca3af', letterSpacing: '0.08em', marginBottom: 5 }}>CHANNEL NORMALISATION</div>
                <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                  {domainFramework.channels.map(c => (
                    <span key={c} style={{ fontSize: 9.5, fontWeight: 700, color: dfColor, background: `${dfColor}12`, border: `1px solid ${dfColor}35`, borderRadius: 3, padding: '1px 6px', fontFamily: '"ui-monospace","SFMono-Regular",monospace' }}>{c}</span>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* ── Physical Silver Schema ── */}
          {domainFramework.entities?.length > 0 && (
            <div style={{ padding: '8px 14px 12px', background: 'white' }}>
              <div style={{ fontSize: 9.5, fontWeight: 700, color: '#9ca3af', letterSpacing: '0.08em', marginBottom: 7 }}>PHYSICAL SILVER SCHEMA</div>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 7, maxHeight: 340, overflowY: 'auto' }}>
                {domainFramework.entities.map(entity => {
                  const typeColor = entity.type === 'dimension' ? dfColor
                    : entity.type === 'event' ? '#d97706'
                    : '#059669'
                  const typeBg = entity.type === 'dimension' ? `${dfColor}15`
                    : entity.type === 'event' ? '#fef3c7'
                    : '#d1fae5'
                  return (
                    <div key={entity.name} style={{ background: '#fafafa', border: '1px solid #e5e7eb', borderRadius: 4, padding: '7px 8px' }}>
                      {/* Entity header */}
                      <div style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 2 }}>
                        <span style={{ fontSize: 9, fontWeight: 700, color: '#111827', fontFamily: '"ui-monospace","SFMono-Regular",monospace', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {entity.name}
                        </span>
                        <span style={{ fontSize: 7.5, fontWeight: 700, color: typeColor, background: typeBg, borderRadius: 2, padding: '1px 4px', textTransform: 'uppercase', flexShrink: 0 }}>
                          {entity.type}
                        </span>
                      </div>
                      <div style={{ fontSize: 8, color: '#9ca3af', fontStyle: 'italic', marginBottom: 5 }}>{entity.grain}</div>
                      {/* Column rows */}
                      <div style={{ borderTop: '1px solid #e5e7eb', paddingTop: 4 }}>
                        {entity.columns.map(col => (
                          <div key={col.name} style={{ display: 'flex', alignItems: 'center', gap: 3, marginBottom: 1.5 }}>
                            <span style={{ fontSize: 9, fontFamily: '"ui-monospace","SFMono-Regular",monospace', color: col.pk ? '#111827' : '#4b5563', fontWeight: col.pk ? 700 : 400, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                              {col.name}
                            </span>
                            <span style={{ fontSize: 7.5, color: '#9ca3af', flexShrink: 0 }}>{col.type}</span>
                            {col.pk && (
                              <span style={{ fontSize: 7, fontWeight: 700, color: '#7c3aed', background: '#ede9fe', borderRadius: 2, padding: '0 3px', flexShrink: 0 }}>PK</span>
                            )}
                            {col.fk && (
                              <span title={`→ ${col.fk_ref}`} style={{ fontSize: 7, fontWeight: 700, color: '#0891b2', background: '#e0f2fe', borderRadius: 2, padding: '0 3px', flexShrink: 0, cursor: 'default' }}>FK</span>
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  )
                })}
              </div>
              {/* Legend */}
              <div style={{ display: 'flex', gap: 10, marginTop: 7, alignItems: 'center' }}>
                <span style={{ fontSize: 8.5, color: '#9ca3af' }}>Legend:</span>
                <span style={{ fontSize: 7.5, fontWeight: 700, color: '#7c3aed', background: '#ede9fe', borderRadius: 2, padding: '0 4px' }}>PK</span>
                <span style={{ fontSize: 8.5, color: '#9ca3af' }}>Primary key</span>
                <span style={{ fontSize: 7.5, fontWeight: 700, color: '#0891b2', background: '#e0f2fe', borderRadius: 2, padding: '0 4px' }}>FK</span>
                <span style={{ fontSize: 8.5, color: '#9ca3af' }}>Foreign key (hover for ref)</span>
                <div style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>
                  {[['dimension', dfColor, `${dfColor}15`], ['event', '#d97706', '#fef3c7'], ['aggregate', '#059669', '#d1fae5']].map(([label, color, bg]) => (
                    <span key={label} style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
                      <span style={{ fontSize: 7.5, fontWeight: 700, color, background: bg, borderRadius: 2, padding: '0 4px', textTransform: 'uppercase' }}>{label}</span>
                    </span>
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>
      )}

      {narrativeText && (
        isSilver && narrativeBullets.length > 1 ? (
          <ul className="sttm-card-bullets sttm-card-narrative-bullets">
            {narrativeBullets.map((b, i) => <li key={i}>{b}</li>)}
          </ul>
        ) : (
          <div className="sttm-card-summary">{narrativeText}</div>
        )
      )}

      {silver_sources?.length > 0 && (
        <ul className="sttm-card-bullets">
          <li>
            <span className="sttm-bullet-label">Silver sources:</span>{' '}
            {silver_sources.join(', ')}
          </li>
          {gold_targets?.length > 0 && (
            <li>
              <span className="sttm-bullet-label">Gold targets:</span>{' '}
              {gold_targets.join(', ')}
            </li>
          )}
          {derived?.length > 0 && derived.map((d, i) => (
            <li key={i}>
              <span className="sttm-bullet-label">Derived:</span>{' '}
              {d.target?.split('.').pop() || d.target} = {d.transform || d.notes}
            </li>
          ))}
        </ul>
      )}

      {variant === 'silver' && lineage_summary?.length > 0 && (
        <ul className="sttm-card-bullets">
          {lineage_summary.map((s, i) => <li key={i}>{s}</li>)}
        </ul>
      )}

      <div className="sttm-table-wrap">
        <div className="sttm-table-header">
          <span className="sttm-table-title">{header || 'STTM mappings'}</span>
          <span className="sttm-table-count">
            {mapping_count ?? mappings.length} mappings
          </span>
        </div>
        <div className="sttm-table-scroll">
          <table className="sttm-table">
            <thead>
              <tr>
                <th>SOURCE COLUMN</th>
                <th>TRANSFORM</th>
                <th>TARGET COLUMN</th>
                <th>TARGET TABLE</th>
              </tr>
            </thead>
            <tbody>
              {mappings.map((m, i) => (
                <tr key={i}>
                  <td>
                    <code>{stripCatalogPrefix(m.source_column) || '(derived)'}</code>
                  </td>
                  <td className="sttm-transform">
                    <em>{m.transform || m.notes || ''}</em>
                  </td>
                  <td>
                    <code className="sttm-target-col">{stripCatalogPrefix(m.target_column)}</code>
                  </td>
                  <td>{stripCatalogPrefix(m.target_table)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {isSilver && lineageChart && (
        <div className="sttm-lineage-wrap">
          <div className="sttm-lineage-header">
            <span className="sttm-lineage-title">Lineage</span>
            <span className="sttm-lineage-sub">Bronze → Silver → Gold</span>
          </div>
          <div className="sttm-lineage-diagram">
            <MermaidBlock chart={lineageChart} />
          </div>
        </div>
      )}
    </div>
  )
}
