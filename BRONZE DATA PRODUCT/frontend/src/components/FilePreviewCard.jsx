import React from 'react'

const COL_LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'

function colLetter(i) {
  return COL_LETTERS[i] || String(i + 1)
}

function WordPreview({ preview, fileName }) {
  const text = typeof preview === 'object' ? preview?.text_preview : String(preview || '')
  const wordCount = typeof preview === 'object' && preview?.word_count ? preview.word_count : (text ? text.split(/\s+/).length : 0)

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
        <p className="fp-word-preview">{text || 'Document content read by agent.'}</p>
        <div className="fp-footer">
          <span className="fp-parsed">✓ {wordCount.toLocaleString()} words parsed</span>
          <span className="fp-read">Read by agent</span>
        </div>
      </div>
    </div>
  )
}

function TextPreview({ preview, fileName, fileType }) {
  const text = typeof preview === 'object' ? (preview?.text_preview || JSON.stringify(preview, null, 2)) : String(preview || '')

  return (
    <div className="fp-card">
      <div className="fp-header">
        <span className="fp-filename">{fileName}</span>
        <span className="fp-badge">{String(fileType || 'file').toUpperCase()}</span>
      </div>
      <div className="fp-word-body">
        <p className="fp-word-preview" style={{ fontFamily: 'monospace', whiteSpace: 'pre-wrap', fontSize: '12px' }}>
          {text.slice(0, 1000) || 'File ready for agent processing.'}
        </p>
        <div className="fp-footer">
          <span className="fp-parsed">✓ File attached successfully</span>
          <span className="fp-read">Ready to process</span>
        </div>
      </div>
    </div>
  )
}

function TablePreview({ preview, fileName }) {
  const columns = Array.isArray(preview?.columns) ? preview.columns : []
  const rows = Array.isArray(preview?.rows) ? preview.rows : []
  const rowCount = preview?.row_count ?? rows.length
  const sheetName = preview?.sheet_name || 'Sheet1'

  if (columns.length === 0) {
    return <TextPreview preview={preview} fileName={fileName} fileType="Spreadsheet" />
  }

  const displayCols = columns.slice(0, 6)
  const displayRows = rows.slice(0, 5)

  return (
    <div className="fp-card">
      <div className="fp-header">
        <span className="fp-filename">{fileName}</span>
        {sheetName && <span className="fp-badge">{sheetName}</span>}
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
                <td key={i} className="fp-td fp-td-header">{String(col ?? '')}</td>
              ))}
            </tr>
            {/* data rows */}
            {displayRows.map((row, ri) => (
              <tr key={ri} className={ri % 2 === 0 ? 'fp-tr-even' : ''}>
                <td className="fp-td fp-td-idx">{ri + 2}</td>
                {displayCols.map((_, ci) => (
                  <td key={ci} className="fp-td">{Array.isArray(row) ? String(row[ci] ?? '') : ''}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="fp-footer">
        <span className="fp-parsed">✓ {columns.length} columns parsed · {rowCount?.toLocaleString()} rows</span>
        <span className="fp-read">Read by agent</span>
      </div>
    </div>
  )
}

export default function FilePreviewCard({ fileName, fileType, preview, refId, onUseFile, onDismiss }) {
  const isTable = (fileType === 'xlsx' || fileType === 'xls' || fileType === 'csv') && Array.isArray(preview?.columns) && preview.columns.length > 0
  const isWord = fileType === 'docx'

  return (
    <div className="fp-wrap">
      {isWord ? (
        <WordPreview preview={preview} fileName={fileName} />
      ) : isTable ? (
        <TablePreview preview={preview} fileName={fileName} />
      ) : (
        <TextPreview preview={preview} fileName={fileName} fileType={fileType} />
      )}
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
