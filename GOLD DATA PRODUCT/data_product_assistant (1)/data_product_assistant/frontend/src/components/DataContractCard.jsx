import { useState } from 'react'

const FileCodeIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
    <polyline points="14 2 14 8 20 8" />
    <path d="m10 13-2 2 2 2" />
    <path d="m14 13 2 2-2 2" />
  </svg>
)

const CheckIcon = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3">
    <polyline points="20 6 9 17 4 12" />
  </svg>
)

const CopyIcon = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
  </svg>
)

export default function DataContractCard({ view }) {
  const [activeTab, setActiveTab] = useState('overview')
  const [copied, setCopied] = useState(false)

  if (!view) return null

  const info = view.info || {}
  const servicelevels = view.servicelevels || {}
  const models = view.models || {}
  const quality = view.quality || []
  const yamlText = view.yaml_text || ''

  const handleCopy = () => {
    if (!yamlText) return
    navigator.clipboard.writeText(yamlText)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div style={{
      border: '1px solid #7500C0',
      borderRadius: 8,
      margin: '12px 0',
      overflow: 'hidden',
      background: '#ffffff',
      fontSize: 12,
      fontFamily: 'Inter, system-ui, -apple-system, sans-serif',
      boxShadow: '0 2px 8px rgba(117, 0, 192, 0.08)',
    }}>
      {/* ── Top Header Bar ── */}
      <div style={{
        background: 'linear-gradient(135deg, #460073 0%, #7500C0 100%)',
        color: '#ffffff',
        padding: '12px 16px',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        flexWrap: 'wrap',
        gap: 8,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{
            background: 'rgba(255, 255, 255, 0.2)',
            borderRadius: 6,
            padding: '4px 8px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}>
            <FileCodeIcon />
          </div>
          <div>
            <div style={{ fontSize: 13, fontWeight: 700, letterSpacing: '0.01em' }}>
              {info.title || 'Gold Layer Data Contract'}
            </div>
            <div style={{ fontSize: 10.5, color: '#E6DCFF', fontFamily: 'monospace' }}>
              {view.id || 'urn:datacontract:gold'}
            </div>
          </div>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{
            fontSize: 9.5,
            fontWeight: 700,
            background: '#A100FF',
            color: '#ffffff',
            padding: '2px 8px',
            borderRadius: 12,
            border: '1px solid #C2A3FF',
          }}>
            v{info.version || '1.0.0'}
          </span>
          <span style={{
            fontSize: 9.5,
            fontWeight: 700,
            background: '#10B981',
            color: '#ffffff',
            padding: '2px 8px',
            borderRadius: 12,
          }}>
            {info.status || 'ACTIVE'}
          </span>
        </div>
      </div>

      {/* ── Navigation Tabs ── */}
      <div style={{
        display: 'flex',
        background: '#E6DCFF',
        borderBottom: '1px solid #C2A3FF',
        padding: '0 8px',
        gap: 4,
      }}>
        {[
          { id: 'overview', label: 'Overview & SLAs' },
          { id: 'schema', label: `Models & Fields (${Object.keys(models).length})` },
          { id: 'quality', label: `Quality Rules (${quality.length})` },
          { id: 'yaml', label: 'YAML Spec' },
        ].map(tab => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            style={{
              background: activeTab === tab.id ? '#ffffff' : 'transparent',
              color: '#460073',
              border: 'none',
              borderTopLeftRadius: 6,
              borderTopRightRadius: 6,
              padding: '8px 12px',
              fontSize: 11,
              fontWeight: activeTab === tab.id ? 700 : 500,
              cursor: 'pointer',
              borderBottom: activeTab === tab.id ? '2px solid #7500C0' : '2px solid transparent',
              transition: 'all 0.15s ease',
            }}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* ── Tab Content ── */}
      <div style={{ padding: 14 }}>
        {/* TAB 1: OVERVIEW */}
        {activeTab === 'overview' && (
          <div>
            <p style={{ color: '#4b5563', lineHeight: 1.5, marginBottom: 12 }}>
              {info.description || 'Gold Layer Data Contract governing consumption-ready schemas and quality expectations.'}
            </p>

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 10, marginBottom: 14 }}>
              <div style={{ background: '#f9fafb', border: '1px solid #e5e7eb', borderRadius: 6, padding: 10 }}>
                <div style={{ fontSize: 10, color: '#6b7280', fontWeight: 600, textTransform: 'uppercase' }}>Domain Owner</div>
                <div style={{ fontWeight: 700, color: '#111827', marginTop: 2 }}>{info.owner || 'Data Governance'}</div>
              </div>
              <div style={{ background: '#f9fafb', border: '1px solid #e5e7eb', borderRadius: 6, padding: 10 }}>
                <div style={{ fontSize: 10, color: '#6b7280', fontWeight: 600, textTransform: 'uppercase' }}>Freshness SLA</div>
                <div style={{ fontWeight: 700, color: '#7500C0', marginTop: 2 }}>{servicelevels.freshness?.schedule || 'DAILY_BATCH'} ({servicelevels.freshness?.maxLag || '24h lag'})</div>
              </div>
              <div style={{ background: '#f9fafb', border: '1px solid #e5e7eb', borderRadius: 6, padding: 10 }}>
                <div style={{ fontSize: 10, color: '#6b7280', fontWeight: 600, textTransform: 'uppercase' }}>Target Dataset</div>
                <div style={{ fontWeight: 700, color: '#111827', fontFamily: 'monospace', marginTop: 2 }}>{info.target_dataset || 'gold'}</div>
              </div>
            </div>

            <div style={{ marginTop: 10 }}>
              <div style={{ fontSize: 10.5, fontWeight: 700, color: '#374151', marginBottom: 6, textTransform: 'uppercase' }}>Governance & Standards</div>
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                {(info.standards || ['BigQuery Native', 'Knowledge Catalog']).map(s => (
                  <span key={s} style={{
                    background: '#E6DCFF',
                    color: '#460073',
                    border: '1px solid #C2A3FF',
                    borderRadius: 4,
                    padding: '2px 8px',
                    fontSize: 10.5,
                    fontWeight: 600,
                  }}>
                    {s}
                  </span>
                ))}
              </div>
            </div>
          </div>
        )}

        {/* TAB 2: SCHEMA & MODELS */}
        {activeTab === 'schema' && (
          <div>
            {Object.entries(models).map(([modelName, modelDef]) => (
              <div key={modelName} style={{ marginBottom: 14, border: '1px solid #e5e7eb', borderRadius: 6, overflow: 'hidden' }}>
                <div style={{ background: '#f8fafc', padding: '8px 12px', borderBottom: '1px solid #e5e7eb', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <span style={{ fontWeight: 700, color: '#460073', fontFamily: 'monospace' }}>{modelDef.physicalName || modelName}</span>
                  <span style={{ fontSize: 10, color: '#6b7280' }}>{modelDef.description}</span>
                </div>
                <div style={{ overflowX: 'auto' }}>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
                    <thead>
                      <tr style={{ background: '#f1f5f9', color: '#475569', textAlign: 'left', borderBottom: '1px solid #e2e8f0' }}>
                        <th style={{ padding: '6px 10px' }}>Field</th>
                        <th style={{ padding: '6px 10px' }}>Type</th>
                        <th style={{ padding: '6px 10px' }}>Flags</th>
                        <th style={{ padding: '6px 10px' }}>Description</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(modelDef.fields || {}).map(([fName, fDef]) => (
                        <tr key={fName} style={{ borderBottom: '1px solid #f1f5f9' }}>
                          <td style={{ padding: '6px 10px', fontFamily: 'monospace', fontWeight: 600, color: '#0f172a' }}>{fName}</td>
                          <td style={{ padding: '6px 10px', fontFamily: 'monospace', color: '#7500C0' }}>{fDef.type}</td>
                          <td style={{ padding: '6px 10px' }}>
                            {fDef.primary && <span style={{ background: '#7500C0', color: '#fff', fontSize: 8.5, padding: '1px 5px', borderRadius: 3, marginRight: 4, fontWeight: 700 }}>PK</span>}
                            {fDef.references && <span style={{ background: '#0284c7', color: '#fff', fontSize: 8.5, padding: '1px 5px', borderRadius: 3, marginRight: 4, fontWeight: 700 }}>FK ({fDef.references})</span>}
                            {fDef.pii && <span style={{ background: '#dc2626', color: '#fff', fontSize: 8.5, padding: '1px 5px', borderRadius: 3, fontWeight: 700 }}>PII</span>}
                          </td>
                          <td style={{ padding: '6px 10px', color: '#64748b' }}>{fDef.description}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ))}
          </div>
        )}

        {/* TAB 3: QUALITY RULES */}
        {activeTab === 'quality' && (
          <div>
            <p style={{ color: '#4b5563', marginBottom: 10 }}>
              Automated data quality assertions attached to this Gold contract:
            </p>
            {quality.length === 0 ? (
              <div style={{ color: '#9ca3af', fontStyle: 'italic' }}>No custom quality assertions defined.</div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {quality.map((q, idx) => (
                  <div key={idx} style={{ background: '#f8fafc', border: '1px solid #e2e8f0', borderLeft: '4px solid #7500C0', borderRadius: 4, padding: 10 }}>
                    <div style={{ fontWeight: 600, color: '#0f172a', marginBottom: 3 }}>{q.description}</div>
                    <code style={{ background: '#E6DCFF', color: '#460073', padding: '2px 6px', borderRadius: 4, fontSize: 10.5 }}>
                      ASSERT: {q.mustBe}
                    </code>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* TAB 4: RAW YAML */}
        {activeTab === 'yaml' && (
          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
              <span style={{ fontSize: 10.5, color: '#6b7280', fontWeight: 600 }}>OpenDataContract Standard v0.9.3</span>
              <button
                onClick={handleCopy}
                style={{
                  background: copied ? '#10B981' : '#7500C0',
                  color: '#ffffff',
                  border: 'none',
                  borderRadius: 4,
                  padding: '4px 10px',
                  fontSize: 10.5,
                  fontWeight: 600,
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 4,
                }}
              >
                {copied ? <CheckIcon /> : <CopyIcon />}
                {copied ? 'Copied!' : 'Copy YAML'}
              </button>
            </div>
            <pre style={{
              background: '#0f172a',
              color: '#f8fafc',
              padding: 12,
              borderRadius: 6,
              fontSize: 11,
              fontFamily: 'Consolas, Monaco, monospace',
              overflowX: 'auto',
              maxHeight: 280,
              lineHeight: 1.45,
            }}>
              {yamlText || '# Data Contract YAML generated successfully\n'}
            </pre>
          </div>
        )}
      </div>
    </div>
  )
}
