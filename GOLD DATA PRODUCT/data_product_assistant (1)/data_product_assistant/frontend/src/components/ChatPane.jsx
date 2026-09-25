import { useRef, useEffect, useState } from 'react'
import MessageRow from './MessageRow'
import InputBar from './InputBar'
import FilePreviewCard from './FilePreviewCard'
import EditRequirementsForm from './EditRequirementsForm'
import DataContractCard, { DEFAULT_GOLD_CONTRACT } from './DataContractCard'
import { BP } from '../theme'
import { Video, Phone, MoreHorizontal } from '../icons'

function ChatTopBar({ activeAgent, hasContract, onOpenContract }) {
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
        {hasContract && (
          <button
            onClick={onOpenContract}
            title="Open & Edit Gold Data Contract"
            style={{
              background: '#E6DCFF',
              color: '#460073',
              border: '1px solid #C2A3FF',
              borderRadius: 6,
              padding: '5px 10px',
              fontSize: 11,
              fontWeight: 700,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: 5,
            }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
              <polyline points="14 2 14 8 20 8" />
              <path d="m10 13-2 2 2 2" />
              <path d="m14 13 2 2-2 2" />
            </svg>
            Gold Data Contract
          </button>
        )}
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
  const [contractModalOpen, setContractModalOpen] = useState(false)

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

  const hasContract = messages.some(m => m.data_contract_view)

  return (
    <div className="flex flex-col" style={{ flex: 1, minWidth: 0, background: BP.panel, position: 'relative' }}>
      <ChatTopBar
        activeAgent={activeAgent}
        hasContract={hasContract}
        onOpenContract={() => setContractModalOpen(true)}
      />

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
            onDismissFile={onDismissFile}
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

      {contractModalOpen && (
        <div
          style={{
            position: 'fixed',
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            background: 'rgba(15, 23, 42, 0.65)',
            backdropFilter: 'blur(4px)',
            zIndex: 9999,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            padding: 20,
          }}
          onClick={(e) => { if (e.target === e.currentTarget) setContractModalOpen(false); }}
        >
          <div style={{
            background: '#ffffff',
            borderRadius: 12,
            width: '100%',
            maxWidth: 1040,
            maxHeight: '92vh',
            overflowY: 'auto',
            boxShadow: '0 25px 50px -12px rgba(0, 0, 0, 0.35)',
          }}>
            <DataContractCard
              view={messages.slice().reverse().find(m => m.data_contract_view)?.data_contract_view || DEFAULT_GOLD_CONTRACT}
              isModal={true}
              onClose={() => setContractModalOpen(false)}
            />
          </div>
        </div>
      )}
    </div>
  )
}
