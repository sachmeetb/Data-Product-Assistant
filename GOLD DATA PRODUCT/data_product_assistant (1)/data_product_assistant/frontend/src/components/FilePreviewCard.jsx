const COL_LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'

function colLetter(i) {
  return COL_LETTERS[i] || String(i + 1)
}

function WordPreview({ preview, fileName }) {
  return (
    <div className="fp-card">
      <div className="fp-header">
        <span className="fp-filename">{fileName}</span>
        <span className="fp-badge">Word Document</span>
      </div>
      <div className="fp-word-body">
        <div className="fp-word-icon">
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
            <polyline points="14 2 14 8 20 8" />
            <line x1="16" y1="13" x2="8" y2="13" />
            <line x1="16" y1="17" x2="8" y2="17" />
            <polyline points="10 9 9 9 8 9" />
          </svg>
        </div>
        <p className="fp-word-preview">{preview.text_preview}</p>
        <div className="fp-footer">
          <span className="fp-parsed">✓ {preview.word_count?.toLocaleString()} words parsed</span>
          <span className="fp-read">Read by agent</span>
        </div>
      </div>
    </div>
  )
}

function TablePreview({ preview, fileName }) {
  const { columns, rows, row_count, sheet_name } = preview
  const displayCols = columns.slice(0, 6)
  const displayRows = rows.slice(0, 5)

  return (
    <div className="fp-card">
      <div className="fp-header">
        <span className="fp-filename">{fileName}</span>
        {sheet_name && <span className="fp-badge">{sheet_name}</span>}
      </div>
      <div className="fp-table-wrap">
        <table className="fp-table">
          <thead>
            <tr>
              <th className="fp-th fp-th-idx" />
              {displayCols.map((_, i) => (
                <th key={i} className="fp-th fp-th-col">{colLetter(i)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {/* header row (row 1) */}
            <tr>
              <td className="fp-td fp-td-idx">1</td>
              {displayCols.map((col, i) => (
                <td key={i} className="fp-td fp-td-header">{col}</td>
              ))}
            </tr>
            {/* data rows */}
            {displayRows.map((row, ri) => (
              <tr key={ri} className={ri % 2 === 0 ? 'fp-tr-even' : ''}>
                <td className="fp-td fp-td-idx">{ri + 2}</td>
                {displayCols.map((_, ci) => (
                  <td key={ci} className="fp-td">{row[ci] ?? ''}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="fp-footer">
        <span className="fp-parsed">✓ {columns.length} columns parsed · {row_count?.toLocaleString()} rows</span>
        <span className="fp-read">Read by agent</span>
      </div>
    </div>
  )
}

export default function FilePreviewCard({ fileName, fileType, preview, refId, onUseFile, onDismiss }) {
  return (
    <div className="fp-wrap">
      {fileType === 'docx'
        ? <WordPreview preview={preview} fileName={fileName} />
        : <TablePreview preview={preview} fileName={fileName} />
      }
      <div className="fp-actions">
        <button className="fp-use-btn" onClick={() => onUseFile(refId)}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
            <polyline points="20 6 9 17 4 12" />
          </svg>
          Use this file instead
        </button>
        <button className="fp-dismiss-btn" onClick={onDismiss}>
          Type instead
        </button>
      </div>
    </div>
  )
}
