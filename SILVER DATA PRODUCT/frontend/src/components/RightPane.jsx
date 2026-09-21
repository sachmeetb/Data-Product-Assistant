const FileIcon = ({ size = 14 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
    <polyline points="14 2 14 8 20 8" />
  </svg>
)

const DownloadIcon = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
    <polyline points="7 10 12 15 17 10" />
    <line x1="12" y1="15" x2="12" y2="3" />
  </svg>
)

function getExtBadgeStyle(ext) {
  const e = (ext || '').toLowerCase()
  if (e === 'pdf')  return { bg: '#fee2e2', color: '#dc2626', border: '#fca5a5' }
  if (e === 'xlsx') return { bg: '#d1fae5', color: '#059669', border: '#6ee7b7' }
  if (e === 'yaml' || e === 'yml') return { bg: '#E6DCFF', color: '#460073', border: '#C2A3FF' }
  if (e === 'sql')  return { bg: '#e0f2fe', color: '#0284c7', border: '#7dd3fc' }
  if (e === 'json') return { bg: '#fef3c7', color: '#d97706', border: '#fcd34d' }
  if (e === 'py')   return { bg: '#fef9c3', color: '#ca8a04', border: '#fde047' }
  return { bg: '#f3f4f6', color: '#4b5563', border: '#e5e7eb' }
}

export default function RightPane({ generatedFiles, onDownload }) {
  return (
    <aside className="right">
      <div className="right-header">
        <FileIcon size={15} />
        Files &amp; Context ({generatedFiles.length})
      </div>
      <div className="right-body">
        {generatedFiles.length === 0 ? (
          <div className="right-empty">
            <div className="right-empty-icon"><FileIcon size={36} /></div>
            <p className="right-empty-title">No files yet</p>
            <p className="right-empty-hint">Generated BigQuery Silver DDL scripts (.sql), Data Contracts (.yaml), STTM Workbooks (.xlsx), and PDF reports will appear here progressively.</p>
            <p className="right-empty-tip">
              <strong>Tip —</strong> select a Bank Profile and business prompt to begin progressive file generation.
            </p>
          </div>
        ) : (
          <div className="right-section">
            <div className="right-section-title generated">
              <span>&#x27F3;</span> PIPELINE ARTIFACTS ({generatedFiles.length})
            </div>
            {generatedFiles.map((f) => {
              const ext = f.name.split('.').pop()?.toUpperCase() || 'FILE'
              const badgeStyle = getExtBadgeStyle(ext)
              return (
                <div key={f.id} className="file-card" style={{ marginBottom: 10 }}>
                  <div className="file-card-icon"><FileIcon /></div>
                  <div className="file-card-body">
                    <span className="file-card-name" style={{ fontWeight: 600 }}>{f.label || f.name}</span>
                    {f.stage && (
                      <span style={{ fontSize: 9, fontWeight: 700, color: '#7500C0', textTransform: 'uppercase', marginBottom: 2 }}>
                        {f.stage}
                      </span>
                    )}
                    <span
                      className="file-card-ext"
                      style={{
                        background: badgeStyle.bg,
                        color: badgeStyle.color,
                        border: `1px solid ${badgeStyle.border}`,
                        padding: '1px 5px',
                        borderRadius: 3,
                        fontWeight: 700,
                        fontSize: 9,
                        width: 'fit-content',
                      }}
                    >
                      {ext}
                    </span>
                  </div>
                  <button
                    className="file-download-btn"
                    onClick={() => onDownload(f.id, f.name)}
                    title={`Download ${f.name}`}
                    style={{ cursor: 'pointer' }}
                  >
                    <DownloadIcon />
                    Download
                  </button>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </aside>
  )
}
