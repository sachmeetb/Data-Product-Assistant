import { useRef, useEffect } from 'react'
import MessageRow from './MessageRow'
import InputBar from './InputBar'
import FilePreviewCard from './FilePreviewCard'
import EditRequirementsForm from './EditRequirementsForm'
import { BP } from '../theme'
import { Video, Phone, MoreHorizontal } from '../icons'

function ChatTopBar({ activeAgent }) {
  return (
    <div className="flex items-center justify-between px-5 shrink-0 border-b"
      style={{ height: 56, background: BP.greenDeep, borderColor: BP.greenDeeper }}>
      <div className="flex items-center gap-3">
        <div className="flex items-center justify-center font-semibold text-white shrink-0"
          style={{ width: 32, height: 32, borderRadius: '50%', background: BP.green, fontSize: 12 }}>DA</div>
        <div>
          <div className="text-white font-semibold t-14 leading-tight flex items-center gap-2">
            Data Product Assistant
            <span style={{ width: 7, height: 7, borderRadius: '50%', background: '#5DD896' }} />
          </div>
          <div className="t-11" style={{ color: 'rgba(255,255,255,0.65)' }}>
            Powered by Accenture Agentic Platform
            {activeAgent && activeAgent !== 'Data Product Assistant' && (
              <span style={{ color: BP.yellow, marginLeft: 8 }}>· {activeAgent}</span>
            )}
          </div>
        </div>
      </div>
      <div className="flex items-center gap-3" style={{ color: 'rgba(255,255,255,0.7)' }}>
        <Video size={17} /><Phone size={17} /><MoreHorizontal size={17} />
      </div>
    </div>
  )
}

export default function ChatPane({
  messages, onSend, onChipClick, sending, inputLocked,
  allowUpload, onUpload, uploading,
  pendingFile, onUseFile, onDismissFile,
  editFormOpen, requirementData, glossaryData, onEditSubmit, onEditClose,
  activeAgent, currentActivity,
}) {
  const bottomRef = useRef(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const handleChipClick = onChipClick || onSend

  const lastAgentMsg = [...messages].reverse().find(m => m.role === 'agent' && !m.loading)
  const hasChips = lastAgentMsg?.chips?.length > 0

  const inputPlaceholder = inputLocked
    ? 'Pick a starting point above to begin…'
    : hasChips
      ? 'Tap a chip above to respond…'
      : 'Message Data Product Assistant…'

  return (
    <div className="flex flex-col" style={{ flex: 1, minWidth: 0, background: BP.panel, position: 'relative' }}>
      <ChatTopBar activeAgent={activeAgent} />

      <div id="chat-messages"
        className="messages overflow-y-auto px-6 py-5 space-y-5"
        style={{ flex: 1, background: BP.panel }}>
        {messages.map(msg => (
          <MessageRow key={msg.id} msg={msg} onChipClick={handleChipClick} />
        ))}
        <div ref={bottomRef} />
      </div>

      {pendingFile && (
        <div style={{ padding: '0 24px 10px' }}>
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

      {/* Activity strip */}
      <div className="flex items-center gap-2 px-5 shrink-0 border-t"
        style={{ background: '#F5EDFF', borderColor: BP.border, height: 38 }}>
        {currentActivity ? (
          <>
            <span style={{ width: 7, height: 7, borderRadius: '50%', background: BP.green }} className="animate-pulse" />
            <div className="t-12" style={{ color: BP.greenDark, fontWeight: 600 }}>{currentActivity}</div>
          </>
        ) : (
          <div className="t-12" style={{ color: BP.textMuted }}>Idle</div>
        )}
      </div>

      <InputBar
        onSend={onSend}
        disabled={sending || inputLocked}
        placeholder={inputPlaceholder}
        allowUpload={allowUpload}
        onUpload={onUpload}
        uploading={uploading}
      />
    </div>
  )
}
