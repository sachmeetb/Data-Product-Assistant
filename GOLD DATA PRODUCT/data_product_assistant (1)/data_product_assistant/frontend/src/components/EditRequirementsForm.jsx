import { useState } from 'react'

const FRESHNESS_OPTIONS = ['', 'real-time', 'hourly', 'daily', 'weekly', 'monthly', 'quarterly']

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

  // Track per-entry glossary descriptions. Keyed by entry name so we can
  // diff against the original on submit and only forward changed descriptions.
  const originalDescriptions = (glossary?.entries || []).reduce((acc, e) => {
    acc[e.name] = e.description || ''
    return acc
  }, {})
  const [glossEdits, setGlossEdits] = useState(originalDescriptions)

  const setGloss = (name, val) =>
    setGlossEdits(prev => ({ ...prev, [name]: val }))

  const set = (key, val) => setForm(prev => ({ ...prev, [key]: val }))

  const handleSubmit = (e) => {
    e.preventDefault()
    const updates = { ...form }
    // Glossary diff — only entries whose description actually changed
    const changed = Object.entries(glossEdits)
      .filter(([name, desc]) => (originalDescriptions[name] || '') !== (desc || ''))
      .map(([name, desc]) => `${name}: ${desc}`)
    if (changed.length) {
      updates['Glossary Definitions'] = changed.join(' | ')
    }
    onSubmit(updates)
  }

  return (
    <div className="erf-overlay">
      <div className="erf-panel">
        <div className="erf-header">
          <div className="erf-title">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
              <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
              <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
            </svg>
            Edit Requirement
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
                placeholder="e.g. Weekly Sales Performance" />
            </div>

            <div className="erf-field">
              <label className="erf-label">Domain</label>
              <input className="erf-input" value={form['Domain']}
                onChange={e => set('Domain', e.target.value)}
                placeholder="e.g. Sales, Marketing, C&P" />
            </div>

            <div className="erf-field">
              <label className="erf-label">Consumer Role</label>
              <input className="erf-input" value={form['Consumer Role']}
                onChange={e => set('Consumer Role', e.target.value)}
                placeholder="e.g. Data Analyst, Regional Manager" />
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
                <span className="erf-hint">comma-separated, e.g. Net Revenue (KPI), Region (attribute)</span>
              </label>
              <textarea className="erf-textarea" rows={3} value={form['Data Points']}
                onChange={e => set('Data Points', e.target.value)}
                placeholder="Units Sold, Net Revenue GBP, Region..." />
            </div>

            <div className="erf-field erf-field-full">
              <label className="erf-label">
                Granularity (Dimensions)
                <span className="erf-hint">comma-separated dimensions</span>
              </label>
              <input className="erf-input" value={form['Granularity']}
                onChange={e => set('Granularity', e.target.value)}
                placeholder="e.g. Region, Country, Product Category, Week" />
            </div>

            <div className="erf-field">
              <label className="erf-label">Data Sources</label>
              <input className="erf-input" value={form['Data Sources']}
                onChange={e => set('Data Sources', e.target.value)}
                placeholder="e.g. Salesforce, SAP, sample.xlsx" />
            </div>

            <div className="erf-field">
              <label className="erf-label">
                Filters
                <span className="erf-hint">e.g. region include UK</span>
              </label>
              <input className="erf-input" value={form['Filters']}
                onChange={e => set('Filters', e.target.value)}
                placeholder="e.g. order_status exclude cancelled" />
            </div>
          </div>

          {glossary?.entries?.length > 0 && (
            <div className="erf-gloss-section">
              <div className="erf-gloss-title">
                Glossary Definitions
                <span className="erf-hint">edit any description below — leave others untouched</span>
              </div>
              {glossary.entries.map((entry, i) => (
                <div key={i} className="erf-gloss-row">
                  <div className="erf-gloss-name">{entry.name}</div>
                  <textarea
                    className="erf-textarea"
                    rows={2}
                    value={glossEdits[entry.name] ?? entry.description ?? ''}
                    onChange={e => setGloss(entry.name, e.target.value)}
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
