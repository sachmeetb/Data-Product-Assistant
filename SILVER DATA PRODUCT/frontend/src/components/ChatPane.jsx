import { useRef, useEffect, useState } from 'react'
import MessageRow from './MessageRow'
import InputBar from './InputBar'
import FilePreviewCard from './FilePreviewCard'
import EditRequirementsForm from './EditRequirementsForm'

export default function ChatPane({
  messages, onSend, onChipClick, sending, inputLocked,
  allowUpload, onUpload, uploading,
  pendingFile, onUseFile, onDismissFile,
  editFormOpen, requirementData, glossaryData, onEditSubmit, onEditClose,
}) {
  const bottomRef = useRef(null)
  const [downloading, setDownloading] = useState(false)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  async function downloadPDF() {
    const el = document.getElementById('chat-messages')
    if (!el || downloading) return
    setDownloading(true)
    el.classList.add('pdf-capture')
    const date = new Date().toISOString().slice(0, 10)
    const opt = {
      margin: [14, 14, 14, 14],
      filename: `conversation-${date}.pdf`,
      image: { type: 'jpeg', quality: 0.98 },
      html2canvas: { scale: 2, useCORS: true, logging: false, backgroundColor: '#f5f5f5' },
      jsPDF: { unit: 'mm', format: 'a4', orientation: 'portrait' },
      pagebreak: { mode: ['css', 'legacy'] },
    }
    try {
      const { default: html2pdf } = await import('html2pdf.js')
      await html2pdf().set(opt).from(el).save()
    } finally {
      el.classList.remove('pdf-capture')
      setDownloading(false)
    }
  }

  // Use the App-level chip handler when provided, fall back to onSend
  const handleChipClick = onChipClick || onSend

  const lastAgentMsg = [...messages].reverse().find(m => m.role === 'agent' && !m.loading)
  const hasChips = lastAgentMsg?.chips?.length > 0
  const isChallenger = (lastAgentMsg?.agent || '').toLowerCase().includes('challenger')

  let statusBar = null
  if (isChallenger) {
    const statusText = hasChips ? 'awaiting decision' : 'review complete'
    statusBar = (
      <div className="agent-status-bar">
        <span className="agent-status-dot" />
        <span className="agent-status-name">Challenger Agent</span>
        <span className="agent-status-sep">·</span>
        <span className="agent-status-state">{statusText}</span>
      </div>
    )
  }

  const inputPlaceholder = hasChips
    ? 'Tap a chip above to respond…'
    : sending || !messages.find(m => m.startingPoint)
      ? 'Message DATA DOMAIN SILVER AGENT...'
      : 'Pick a starting point above to begin…'

  return (
    <main className="chat">
      <header className="chat-header">
        <div className="chat-header-left">
          <div className="da-avatar">DA</div>
          <div>
            <div className="chat-title">
              DATA DOMAIN SILVER AGENT <span className="online-dot" />
            </div>
            <div className="chat-subtitle">Powered by Enterprise Data Domain Architecture &amp; BigQuery</div>
          </div>
        </div>
        <div className="header-actions">
          <button className="header-download-btn" onClick={downloadPDF} disabled={downloading} title="Download conversation as PDF">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
              <polyline points="7 10 12 15 17 10" />
              <line x1="12" y1="15" x2="12" y2="3" />
            </svg>
            {downloading ? 'Generating…' : 'Download'}
          </button>
          <button className="header-btn" title="Video">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <polygon points="23 7 16 12 23 17 23 7" /><rect x="1" y="5" width="15" height="14" rx="2" />
            </svg>
          </button>
          <button className="header-btn" title="Call">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07A19.5 19.5 0 0 1 4.69 12 19.79 19.79 0 0 1 1.61 3.41 2 2 0 0 1 3.6 1.25h3a2 2 0 0 1 2 1.72c.13.96.36 1.9.68 2.81a2 2 0 0 1-.45 2.11L7.91 9a16 16 0 0 0 6.09 6.09l1.27-1.27a2 2 0 0 1 2.11-.45c.91.32 1.85.55 2.81.68A2 2 0 0 1 22 16.92z" />
            </svg>
          </button>
          <button className="header-btn" title="More">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor">
              <circle cx="5" cy="12" r="2" /><circle cx="12" cy="12" r="2" /><circle cx="19" cy="12" r="2" />
            </svg>
          </button>
        </div>
      </header>

      <div className="messages" id="chat-messages">
        {messages.map(msg => (
          <MessageRow key={msg.id} msg={msg} onChipClick={handleChipClick} />
        ))}
        <div ref={bottomRef} />
      </div>

      {pendingFile && (
        <div className="fp-chat-preview">
          <FilePreviewCard
            fileName={pendingFile.fileName}
            fileType={pendingFile.fileType}
            preview={pendingFile.preview}
            refId={pendingFile.refId}
            onUseFile={onUseFile}
            onDismiss={onDismissFile}
          />
        </div>
      )}

      {editFormOpen && requirementData && (
        <EditRequirementsForm
          data={requirementData}
          glossary={glossaryData}
          onSubmit={onEditSubmit}
          onClose={onEditClose}
        />
      )}

      {statusBar}
      <InputBar
        onSend={onSend}
        disabled={sending || inputLocked}
        placeholder={inputLocked ? 'Pick a starting point above to begin…' : inputPlaceholder}
        allowUpload={allowUpload}
        onUpload={onUpload}
        uploading={uploading}
      />
    </main>
  )
}
