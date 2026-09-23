import { useState, useEffect, useCallback } from 'react'
import { fetchDomainRegistry, fetchDomainFramework } from '../api/catalog'

const TYPE_LABELS = { dimension: 'Dimension', event: 'Event', aggregate: 'Aggregate' }
const TYPE_COLORS = { dimension: '#6366f1', event: '#059669', aggregate: '#d97706' }

function StatusBadge({ status }) {
  const colors = { green: '#059669', amber: '#d97706', red: '#dc2626' }
  const labels = { green: 'Active', amber: 'Coming Soon', red: 'Out of Scope' }
  return (
    <span className="df-status-badge" style={{ background: colors[status] || '#6b7280' }}>
      {labels[status] || status}
    </span>
  )
}

function EntityTypePill({ type }) {
  return (
    <span className="df-entity-type-pill" style={{ background: TYPE_COLORS[type] || '#6b7280' }}>
      {TYPE_LABELS[type] || type}
    </span>
  )
}

function ColumnRow({ col }) {
  return (
    <tr className="df-col-row">
      <td className="df-col-name">
        {col.is_pk && <span className="df-pk-badge">PK</span>}
        {col.fk_ref && <span className="df-fk-badge">FK</span>}
        {col.name}
      </td>
      <td className="df-col-type">{col.data_type}</td>
      <td className="df-col-null">{col.nullable ? '—' : 'NOT NULL'}</td>
      <td className="df-col-desc">{col.description}</td>
    </tr>
  )
}

function EntityCard({ name, entity }) {
  const [open, setOpen] = useState(false)
  const cols = entity.columns || []
  return (
    <div className="df-entity-card">
      <button className="df-entity-header" onClick={() => setOpen(o => !o)}>
        <div className="df-entity-left">
          <EntityTypePill type={entity.type} />
          <span className="df-entity-name">{name}</span>
        </div>
        <div className="df-entity-right">
          <span className="df-entity-grain">{entity.grain}</span>
          <svg
            className={`df-chevron ${open ? 'open' : ''}`}
            width="14" height="14" viewBox="0 0 24 24"
            fill="none" stroke="currentColor" strokeWidth="2.5"
          >
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </div>
      </button>
      {open && (
        <div className="df-entity-body">
          <table className="df-col-table">
            <thead>
              <tr>
                <th>Column</th><th>Type</th><th>Nullable</th><th>Description</th>
              </tr>
            </thead>
            <tbody>
              {cols.map(c => <ColumnRow key={c.name} col={c} />)}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function DomainDetail({ domain, framework, loading, error }) {
  if (loading) return <div className="df-detail-loading"><div className="df-spinner" />Loading framework…</div>
  if (error)   return <div className="df-detail-error">{error}</div>
  if (!framework) return (
    <div className="df-detail-empty">
      <div className="df-detail-empty-icon">📐</div>
      <p>Select a domain to explore its Silver Layer skeleton.</p>
    </div>
  )

  const entities   = framework.entities || {}
  const types      = framework.entity_types || {}
  const metrics    = framework.derived_metrics || {}
  const sources    = framework.source_platform_mappings || {}

  const groups = [
    { label: 'Dimensions', keys: types.dimensions || [], icon: '🗂' },
    { label: 'Events',     keys: types.events     || [], icon: '⚡' },
    { label: 'Aggregates', keys: types.aggregates || [], icon: '📊' },
  ]

  return (
    <div className="df-detail">
      <div className="df-detail-header">
        <div className="df-detail-dot" style={{ background: domain.color }} />
        <div>
          <h2 className="df-detail-title">{framework.display_name}</h2>
          <p className="df-detail-desc">{framework.description}</p>
        </div>
      </div>

      <div className="df-detail-meta-row">
        <div className="df-meta-chip">
          <span className="df-meta-label">Standards</span>
          <span className="df-meta-value">{(framework.standards || []).join(' · ')}</span>
        </div>
        <div className="df-meta-chip">
          <span className="df-meta-label">Entities</span>
          <span className="df-meta-value">{Object.keys(entities).length}</span>
        </div>
        <div className="df-meta-chip">
          <span className="df-meta-label">Key Metrics</span>
          <span className="df-meta-value">{Object.keys(metrics).join(', ')}</span>
        </div>
      </div>

      <div className="df-hierarchy-row">
        <span className="df-section-label">Hierarchy</span>
        <div className="df-hierarchy">
          {(framework.hierarchy || []).map((h, i) => (
            <span key={h}>
              <span className="df-hier-node">{h}</span>
              {i < framework.hierarchy.length - 1 && (
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#9ca3af" strokeWidth="2">
                  <line x1="5" y1="12" x2="19" y2="12" /><polyline points="12 5 19 12 12 19" />
                </svg>
              )}
            </span>
          ))}
        </div>
      </div>

      <div className="df-entities-section">
        {groups.map(g => g.keys.length > 0 && (
          <div key={g.label} className="df-entity-group">
            <div className="df-entity-group-label">{g.icon} {g.label} ({g.keys.length})</div>
            {g.keys.map(k => entities[k] && (
              <EntityCard key={k} name={k} entity={entities[k]} />
            ))}
          </div>
        ))}
      </div>

      {Object.keys(metrics).length > 0 && (
        <div className="df-metrics-section">
          <div className="df-section-label">Derived Metrics (gold layer)</div>
          <div className="df-metrics-grid">
            {Object.entries(metrics).map(([key, m]) => (
              <div key={key} className="df-metric-card">
                <span className="df-metric-name">{key}</span>
                <code className="df-metric-formula">{m.formula}</code>
                <span className="df-metric-desc">{m.description}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {Object.keys(sources).length > 0 && (
        <div className="df-sources-section">
          <div className="df-section-label">Source Platform Mappings</div>
          <div className="df-sources-table-wrap">
            <table className="df-sources-table">
              <thead><tr><th>Source Platform Field</th><th>Silver Entity</th></tr></thead>
              <tbody>
                {Object.entries(sources).map(([src, tgt]) => (
                  <tr key={src}>
                    <td className="df-src-field">{src}</td>
                    <td className="df-src-target">{tgt}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}

export default function DomainFrameworkModal({ onClose }) {
  const [registry, setRegistry]       = useState(null)
  const [regLoading, setRegLoading]   = useState(true)
  const [regError, setRegError]       = useState(null)
  const [selected, setSelected]       = useState(null)
  const [framework, setFramework]     = useState(null)
  const [fwLoading, setFwLoading]     = useState(false)
  const [fwError, setFwError]         = useState(null)
  const [search, setSearch]           = useState('')

  useEffect(() => {
    fetchDomainRegistry()
      .then(data => { setRegistry(data); setRegLoading(false) })
      .catch(err  => { setRegError(err.message); setRegLoading(false) })
  }, [])

  const selectDomain = useCallback((domain) => {
    if (domain.status !== 'green' || !domain.framework_file) return
    setSelected(domain)
    setFramework(null)
    setFwError(null)
    setFwLoading(true)
    fetchDomainFramework(domain.name)
      .then(data => { setFramework(data); setFwLoading(false) })
      .catch(err  => { setFwError(err.message); setFwLoading(false) })
  }, [])

  const allDomains = [
    ...(registry?.domains       || []),
    ...(registry?.amber_domains || []),
  ]

  const filtered = allDomains.filter(d =>
    !search || d.display_name.toLowerCase().includes(search.toLowerCase())
  )

  return (
    <div className="df-overlay" onClick={e => e.target === e.currentTarget && onClose()}>
      <div className="df-modal">

        {/* ── Header ── */}
        <div className="df-modal-header">
          <div className="df-modal-title-wrap">
            <div className="df-modal-icon">📐</div>
            <div>
              <h1 className="df-modal-title">Domain Framework</h1>
              <p className="df-modal-subtitle">Silver Layer skeletons — canonical entity models per subject area</p>
            </div>
          </div>
          <button className="df-close-btn" onClick={onClose}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        <div className="df-modal-body">

          {/* ── Left sidebar: domain list ── */}
          <aside className="df-sidebar">
            <div className="df-sidebar-search-wrap">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
              </svg>
              <input
                className="df-sidebar-search"
                placeholder="Search domains…"
                value={search}
                onChange={e => setSearch(e.target.value)}
              />
            </div>

            {regLoading && <div className="df-sidebar-loading">Loading…</div>}
            {regError   && <div className="df-sidebar-error">{regError}</div>}

            {!regLoading && !regError && (
              <ul className="df-domain-list">
                {filtered.map(d => (
                  <li
                    key={d.name}
                    className={`df-domain-item ${selected?.name === d.name ? 'active' : ''} ${d.status !== 'green' ? 'disabled' : ''}`}
                    onClick={() => selectDomain(d)}
                  >
                    <div className="df-domain-dot" style={{ background: d.color || '#9ca3af' }} />
                    <div className="df-domain-item-body">
                      <span className="df-domain-item-name">{d.display_name}</span>
                      <div className="df-domain-item-meta">
                        <StatusBadge status={d.status} />
                        {d.entity_count && (
                          <span className="df-domain-entity-count">{d.entity_count} entities</span>
                        )}
                      </div>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </aside>

          {/* ── Right detail panel ── */}
          <main className="df-detail-panel">
            <DomainDetail
              domain={selected || {}}
              framework={framework}
              loading={fwLoading}
              error={fwError}
            />
          </main>

        </div>
      </div>
    </div>
  )
}
