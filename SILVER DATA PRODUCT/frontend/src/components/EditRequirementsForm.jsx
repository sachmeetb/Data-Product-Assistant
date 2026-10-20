import { useState, useMemo } from 'react'

const FRESHNESS_OPTIONS = ['', 'real-time', 'hourly', 'daily', 'weekly', 'monthly', 'quarterly']
const SQL_TYPES = ['STRING', 'NUMERIC', 'INT64', 'FLOAT64', 'BOOL', 'DATE', 'TIMESTAMP', 'JSON']

function dpLabel(dp) {
  if (!dp || typeof dp !== 'object') return String(dp)
  const kind = dp.kind === 'kpi' ? 'KPI' : dp.kind === 'attribute' ? 'attribute' : dp.kind
  return `${dp.name} (${kind})`
}

function granLabel(g) {
  if (!g || typeof g !== 'object') return String(g)
  return g.dimension || String(g)
}

function sourceLabel(s) {
  if (!s || typeof s !== 'object') return String(s)
  return s.source_name || String(s)
}

export default function EditRequirementsForm({ data, glossary, onSubmit, onClose }) {
  const [form, setForm] = useState({
    'Use Case Name':   data?.use_case_name   || '',
    'Domain':          data?.domain           || '',
    'Consumer Role':   data?.consumer_role    || '',
    'Data Freshness':  data?.data_freshness   || '',
    'Data Points':     (data?.data_points || data?.kpis || []).map(dpLabel).join(', '),
    'Granularity':     (data?.granularity     || []).map(granLabel).join(', '),
    'Data Sources':    (data?.data_sources    || []).map(sourceLabel).join(', '),
    'Filters':         (data?.filters         || []).map(f =>
      typeof f === 'object' ? `${f.field} ${f.operator} ${f.value}` : String(f)
    ).join(', '),
  })

  // Normalize entries with qualified key (entity_name.name) so same column names in different tables don't collide
  const flatEntries = useMemo(() => {
    if (Array.isArray(glossary?.entities) && glossary.entities.length > 0) {
      const list = []
      for (const ent of glossary.entities) {
        for (const attr of (ent.attributes || [])) {
          if (attr.included !== false) {
            const colName = attr.column_name || attr.name
            list.push({
              ...attr,
              entity_name: ent.table_name,
              name: colName,
              column_name: colName,
              qualified_key: `${ent.table_name}.${colName}`,
            })
          }
        }
      }
      return list
    }
    return (glossary?.entries || []).map(e => ({
      ...e,
      entity_name: e.entity_name || 'slv_entity',
      column_name: e.column_name || e.name,
      qualified_key: `${e.entity_name || 'slv_entity'}.${e.column_name || e.name}`,
    }))
  }, [glossary])

  const originalDescriptions = useMemo(() => {
    return flatEntries.reduce((acc, e) => {
      acc[e.qualified_key] = e.description || ''
      return acc
    }, {})
  }, [flatEntries])

  const [glossEdits, setGlossEdits] = useState(originalDescriptions)
  const [typeEdits, setTypeEdits] = useState(() =>
    flatEntries.reduce((acc, e) => {
      acc[e.qualified_key] = (e.sql_type || e.data_type || 'STRING').toUpperCase()
      return acc
    }, {})
  )
  const [activeEntityFilter, setActiveEntityFilter] = useState('ALL')

  const entityNames = useMemo(
    () => Array.from(new Set(flatEntries.map(e => e.entity_name))),
    [flatEntries]
  )

  const setGloss = (qKey, val) =>
    setGlossEdits(prev => ({ ...prev, [qKey]: val }))

  const setSqlType = (qKey, val) =>
    setTypeEdits(prev => ({ ...prev, [qKey]: val }))

  const set = (key, val) => setForm(prev => ({ ...prev, [key]: val }))

  const handleSubmit = (e) => {
    e.preventDefault()
    const updates = { ...form }
    const changed = Object.entries(glossEdits)
      .filter(([qKey, desc]) => (originalDescriptions[qKey] || '') !== (desc || ''))
      .map(([qKey, desc]) => {
        const colName = qKey.includes('.') ? qKey.split('.').slice(1).join('.') : qKey
        return `${colName}: ${desc}`
      })
    if (changed.length) {
      updates['Glossary Definitions'] = changed.join(' | ')
    }

    // Build structured glossary payload so server updates custom_entities directly
    const updatedEntries = flatEntries.map(entry => ({
      ...entry,
      description: glossEdits[entry.qualified_key] ?? entry.description ?? '',
      sql_type: typeEdits[entry.qualified_key] ?? entry.sql_type ?? 'STRING',
      data_type: typeEdits[entry.qualified_key] ?? entry.sql_type ?? 'STRING',
    }))
    const updatedGlossary = {
      ...(glossary || {}),
      entries: updatedEntries,
    }

    onSubmit(updates, updatedGlossary)
  }

  const visibleEntries = useMemo(
    () => flatEntries.filter(e => activeEntityFilter === 'ALL' || e.entity_name === activeEntityFilter),
    [flatEntries, activeEntityFilter]
  )

  return (
    <div className="erf-overlay">
      <div className="erf-panel">
        <div className="erf-header">
          <div className="erf-title">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
              <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
              <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
            </svg>
            Edit Requirement &amp; Entity Glossary
          </div>
          <button className="erf-close" onClick={onClose} type="button" aria-label="Close">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        <form className="erf-body" onSubmit={handleSubmit}>
          <div className="erf-grid">

            <div className="erf-field">
              <label className="erf-label">Use Case Name</label>
              <input className="erf-input" value={form['Use Case Name']}
                onChange={e => set('Use Case Name', e.target.value)}
                placeholder="e.g. Core Banking Account Balance & Txn Analytics" />
            </div>

            <div className="erf-field">
              <label className="erf-label">Domain</label>
              <input className="erf-input" value={form['Domain']}
                onChange={e => set('Domain', e.target.value)}
                placeholder="e.g. Core Banking, Cards, Payments, Credit Risk" />
            </div>

            <div className="erf-field">
              <label className="erf-label">Consumer Role</label>
              <input className="erf-input" value={form['Consumer Role']}
                onChange={e => set('Consumer Role', e.target.value)}
                placeholder="e.g. Risk Officer, Banking Analyst, Auditor" />
            </div>

            <div className="erf-field">
              <label className="erf-label">Data Freshness</label>
              <select className="erf-select" value={form['Data Freshness']}
                onChange={e => set('Data Freshness', e.target.value)}>
                {FRESHNESS_OPTIONS.map(o => (
                  <option key={o} value={o}>{o || '— select —'}</option>
                ))}
              </select>
            </div>

            <div className="erf-field erf-field-full">
              <label className="erf-label">
                Data Points / Attributes
                <span className="erf-hint">comma-separated, e.g. Net Interest Margin (KPI), Account Type (attribute)</span>
              </label>
              <textarea className="erf-textarea" rows={3} value={form['Data Points']}
                onChange={e => set('Data Points', e.target.value)}
                placeholder="account_id, txn_amount (KPI), current_balance, currency_code, kyc_status..." />
            </div>

            <div className="erf-field erf-field-full">
              <label className="erf-label">
                Granularity (Dimensions)
                <span className="erf-hint">comma-separated dimensions</span>
              </label>
              <input className="erf-input" value={form['Granularity']}
                onChange={e => set('Granularity', e.target.value)}
                placeholder="e.g. Account, Customer, Branch, Currency, Date" />
            </div>

            <div className="erf-field">
              <label className="erf-label">Data Sources</label>
              <input className="erf-input" value={form['Data Sources']}
                onChange={e => set('Data Sources', e.target.value)}
                placeholder="e.g. Flexcube, Temenos, ISO 20022 Wire Feeds" />
            </div>

            <div className="erf-field">
              <label className="erf-label">
                Filters
                <span className="erf-hint">e.g. currency_code include USD, EUR</span>
              </label>
              <input className="erf-input" value={form['Filters']}
                onChange={e => set('Filters', e.target.value)}
                placeholder="e.g. status_cd include ACTIVE" />
            </div>
          </div>

          {flatEntries.length > 0 && (
            <div className="erf-gloss-section">
              <div className="erf-gloss-title" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
                <div>
                  Entity-Scoped Glossary Definitions ({flatEntries.length} attributes)
                  <span className="erf-hint">edit any business definition or SQL type below</span>
                </div>
                {entityNames.length > 1 && (
                  <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                    <button
                      type="button"
                      onClick={() => setActiveEntityFilter('ALL')}
                      style={{
                        fontSize: 11,
                        padding: '2px 8px',
                        borderRadius: 4,
                        border: '1px solid #CBD5E1',
                        background: activeEntityFilter === 'ALL' ? '#7500C0' : '#FFFFFF',
                        color: activeEntityFilter === 'ALL' ? '#FFFFFF' : '#334155',
                        cursor: 'pointer',
                      }}
                    >
                      All ({flatEntries.length})
                    </button>
                    {entityNames.map(entName => (
                      <button
                        key={entName}
                        type="button"
                        onClick={() => setActiveEntityFilter(entName)}
                        style={{
                          fontSize: 11,
                          fontFamily: 'monospace',
                          padding: '2px 8px',
                          borderRadius: 4,
                          border: '1px solid #CBD5E1',
                          background: activeEntityFilter === entName ? '#7500C0' : '#FFFFFF',
                          color: activeEntityFilter === entName ? '#FFFFFF' : '#334155',
                          cursor: 'pointer',
                        }}
                      >
                        {entName}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              {visibleEntries.map((entry) => (
                <div key={entry.qualified_key} className="erf-gloss-row" style={{ display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 10 }}>
                  <div className="erf-gloss-name" style={{ display: 'flex', alignItems: 'center', gap: 6, paddingTop: 0 }}>
                    <span style={{ fontSize: 10.5, color: '#6B21A8', background: '#F3E8FF', padding: '1px 6px', borderRadius: 4, fontFamily: 'monospace' }}>
                      {entry.entity_name}
                    </span>
                    <span style={{ fontFamily: 'monospace', fontWeight: 700 }}>{entry.name}</span>
                    <select
                      value={typeEdits[entry.qualified_key] || 'STRING'}
                      onChange={e => setSqlType(entry.qualified_key, e.target.value)}
                      style={{
                        marginLeft: 'auto',
                        fontSize: 11,
                        fontFamily: 'monospace',
                        padding: '2px 6px',
                        borderRadius: 4,
                        border: '1px solid #CBD5E1',
                      }}
                    >
                      {SQL_TYPES.map(t => (
                        <option key={t} value={t}>{t}</option>
                      ))}
                    </select>
                  </div>
                  <textarea
                    className="erf-textarea"
                    rows={2}
                    value={glossEdits[entry.qualified_key] ?? entry.description ?? ''}
                    onChange={e => setGloss(entry.qualified_key, e.target.value)}
                  />
                </div>
              ))}
            </div>
          )}

          <div className="erf-footer">
            <button type="button" className="erf-cancel" onClick={onClose}>Cancel</button>
            <button type="submit" className="erf-submit">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                <polyline points="20 6 9 17 4 12" />
              </svg>
              Save changes
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
