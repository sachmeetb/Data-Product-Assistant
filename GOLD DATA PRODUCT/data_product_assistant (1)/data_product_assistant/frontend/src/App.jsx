import { useState, useCallback } from 'react'
import NavRail from './components/NavRail'
import ChatSidebar from './components/ChatSidebar'
import ChatPane from './components/ChatPane'
import FilesPanel from './components/FilesPanel'
import TweakModal from './components/TweakModal'
import PhaseStepper from './components/PhaseStepper'
import { BP, phaseForStep, agentMeta } from './theme'
import { Download, RotateCcw } from './icons'
import { sendChatMessage, downloadFile, uploadFile } from './api/chat'

const DDI_CHIP_ADJUST_ER   = 'Adjust the model'
const DDI_CHIP_TWEAK_STTM  = 'Tweak the mapping'

const _PHASE_ORDER = { dpi: 0, designer: 1, builder: 2 }

// Derive phase from the agent name on a single message — more reliable than
// current_step alone because the agent field is always present in messages[].
function phaseFromAgent(agentName) {
  const n = (agentName || '').toLowerCase()
  if (n.includes('pipeline') || n.includes('test agent') || n.includes('publisher') || n.includes('builder')) return 'builder'
  if (n.includes('sttm') || n.includes('gold model') || n.includes('gold er') || n.includes('silver layer') || n.includes('silver transform') || n.includes('designer')) return 'designer'
  return null
}

// Compute the furthest-advanced phase from two signals:
//   1. current_step sent by the backend (phaseForStep)
//   2. the last agent name / card type visible in the conversation
function deriveActivePhase(currentStep, messages, started) {
  if (!started) return null
  const stepPhase = phaseForStep(currentStep)
  const msgPhase = [...messages]
    .reverse()
    .filter(m => m.role === 'agent' && !m.loading)
    .map(m => (m.sttm_view || m.silver_transform_view) ? 'designer' : phaseFromAgent(m.agent))
    .find(p => p !== null) ?? null
  const best = [stepPhase, msgPhase]
    .filter(Boolean)
    .reduce((b, p) => !b || _PHASE_ORDER[p] > _PHASE_ORDER[b] ? p : b, null)
  return best || 'dpi'
}

const UPLOAD_ALLOWED_STEPS = new Set([null, 'initial', 'dpi_clarifying', 'dpi_phase_b'])

function ts() {
  return new Date().toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
}

function initialMessages() {
  return [{
    id: 0, role: 'agent', agent: 'Data Product Assistant',
    text: "Hi — I'm the Data Product Assistant. I help you find, design and build data products. Where would you like to start?",
    chips: [], loading: false, time: ts(), startingPoint: true,
  }]
}

// Login removed — the app opens straight into the workspace with a default user.
const DEFAULT_USER = { email: 'user@accenture.com', name: 'Accenture User', initials: 'AC' }

export default function App() {
  return <Workspace user={DEFAULT_USER} />
}

function Workspace({ user }) {
  const [messages, setMessages]           = useState(initialMessages)
  const [sessionId, setSessionId]             = useState(() => crypto.randomUUID())
  const [generatedFiles, setGeneratedFiles]   = useState([])
  const [sending, setSending]                 = useState(false)
  const [startingPointPicked, setStartingPointPicked] = useState(false)
  const [currentStep, setCurrentStep]         = useState(null)
  const [uploading, setUploading]             = useState(false)
  const [pendingFile, setPendingFile]         = useState(null)
  const [requirementData, setRequirementData] = useState(null)
  const [glossaryData, setGlossaryData]       = useState(null)
  const [editFormOpen, setEditFormOpen]       = useState(false)
  const [tweakMode, setTweakMode]             = useState(null) // 'er' | 'sttm' | null
  const [downloading, setDownloading]         = useState(false)

  const allowUpload = startingPointPicked && UPLOAD_ALLOWED_STEPS.has(currentStep) && !pendingFile

  const activePhase = deriveActivePhase(currentStep, messages, startingPointPicked)
  const lastAgentMsg = [...messages].reverse().find(m => m.role === 'agent' && !m.loading)
  const activeAgent = lastAgentMsg?.agent || 'Data Product Assistant'
  const currentActivity = sending
    ? `${agentMeta(activeAgent).label} · working…`
    : startingPointPicked ? `${agentMeta(activeAgent).label} · ready` : 'Data Product Assistant · ready'

  const handleDownload = useCallback((fileId, fileName) => {
    downloadFile(fileId, fileName).catch(err => console.error('Download failed:', err))
  }, [])

  async function downloadChatPDF() {
    const el = document.getElementById('chat-messages')
    if (!el || downloading) return
    setDownloading(true)
    el.classList.add('pdf-capture')
    const date = new Date().toISOString().slice(0, 10)
    const opt = {
      margin: [14, 14, 14, 14],
      filename: `conversation-${date}.pdf`,
      image: { type: 'jpeg', quality: 0.98 },
      html2canvas: { scale: 2, useCORS: true, logging: false, backgroundColor: '#ffffff' },
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

  const restart = useCallback(() => {
    setMessages(initialMessages())
    setSessionId(crypto.randomUUID())
    setGeneratedFiles([])
    setSending(false)
    setStartingPointPicked(false)
    setCurrentStep(null)
    setUploading(false)
    setPendingFile(null)
    setRequirementData(null)
    setGlossaryData(null)
    setEditFormOpen(false)
    setTweakMode(null)
  }, [])

  const applyResponse = useCallback((data, thinkId) => {
    if (data.current_step) setCurrentStep(data.current_step)

    if (data.messages?.length) {
      const reqData = data.messages.map(m => m.requirement_data).filter(Boolean).at(-1)
      if (reqData) setRequirementData(reqData)
      const gloss = data.messages.map(m => m.glossary).filter(Boolean).at(-1)
      if (gloss) setGlossaryData(gloss)

      setMessages(prev => {
        const withoutLoading = prev
          .filter(m => m.id !== thinkId)
          .map(m => ({ ...m, chips: [] }))
        const newMsgs = data.messages.map((msg, idx) => ({
          id: thinkId + idx,
          role: 'agent',
          agent: msg.agent || 'Data Product Assistant',
          text: msg.text || '',
          chips: msg.chips?.length
            ? msg.chips
            : idx === data.messages.length - 1 ? (data.chips || []) : [],
          discovery_view: msg.discovery_view
            ?? (idx === data.messages.length - 1 ? data.discovery_view : undefined),
          glossary: msg.glossary ?? undefined,
          classification_view: msg.classification_view ?? undefined,
          challenger_view: msg.challenger_view ?? undefined,
          sttm_view: msg.sttm_view ?? undefined,
          silver_transform_view: msg.silver_transform_view ?? undefined,
          data_contract_view: msg.data_contract_view ?? undefined,
          loading: false,
          time: ts(),
        }))
        return [...withoutLoading, ...newMsgs]
      })

      const allFiles = data.messages.flatMap(msg => msg.files || [])
      if (allFiles.length) {
        setGeneratedFiles(prev => {
          const existing = new Set(prev.map(f => f.id))
          return [
            ...prev,
            ...allFiles
              .filter(f => f.id && !existing.has(f.id))
              .map(f => ({
                id: f.id, name: f.name,
                label: f.label || f.name.replace(/-/g, ' ').replace(/\.[^.]+$/, ''),
                meta: `Generated ${ts()}`,
              })),
          ]
        })
      }
    } else {
      setMessages(prev => prev.map(m =>
        m.id === thinkId
          ? { ...m, loading: false, text: data.text || '', chips: data.chips || [],
              discovery_view: data.discovery_view,
              classification_view: data.classification_view }
          : m
      ))
    }
  }, [])

  const sendMessage = useCallback(async (text, opts = {}) => {
    if ((!text.trim() && !opts.fileRefId) || sending) return
    setSending(true)
    setPendingFile(null)

    setMessages(prev => prev.map(m =>
      m.startingPoint ? { ...m, startingPoint: false, startingPointDisabled: true } : m
    ))
    setStartingPointPicked(true)

    const thinkId = Date.now()
    const displayText = opts.fileRefId ? `📎 ${opts.fileName || 'Uploaded file'} — use this file` : text
    setMessages(prev => [
      ...prev,
      { id: thinkId - 1, role: 'user', text: displayText, time: ts() },
      { id: thinkId, role: 'agent', agent: 'Data Product Assistant', loading: true, time: ts() },
    ])

    try {
      const data = await sendChatMessage(sessionId, text, {
        action: opts.action,
        fileRefId: opts.fileRefId,
      })
      setSessionId(data.session_id)
      applyResponse(data, thinkId)
    } catch (err) {
      setMessages(prev => prev.map(m =>
        m.id === thinkId ? { ...m, loading: false, text: `Error: ${err.message}` } : m
      ))
    }

    setSending(false)
  }, [sessionId, sending, applyResponse])

  const handleChipClick = useCallback((label) => {
    if ((label === 'Edit' || label === 'Let me tweak this' || label === 'Let me tweak one') && requirementData) {
      setEditFormOpen(true)
      return
    }
    if (label === DDI_CHIP_ADJUST_ER) { setTweakMode('er'); return }
    if (label === DDI_CHIP_TWEAK_STTM) { setTweakMode('sttm'); return }
    sendMessage(label)
  }, [requirementData, sendMessage])

  const handleTweakSubmit = useCallback((text) => {
    const mode = tweakMode
    setTweakMode(null)
    if (!mode || !text?.trim()) return
    sendMessage(text, { action: mode === 'sttm' ? 'tweak_sttm' : 'tweak_er' })
  }, [tweakMode, sendMessage])

  const handleTweakClose = useCallback(() => setTweakMode(null), [])

  const handleEditSubmit = useCallback((updates) => {
    setEditFormOpen(false)
    const lines = Object.entries(updates)
      .filter(([, v]) => v !== null && v !== undefined && String(v).trim() !== '')
      .map(([k, v]) => {
        if (k === 'Glossary Definitions') {
          return `- Update these data point descriptions (one per pipe): ${v}`
        }
        return `- ${k}: ${v}`
      })
    const msg = `Please update my requirement with the following changes:\n${lines.join('\n')}`
    sendMessage(msg, { action: 'edit' })
  }, [sendMessage])

  const handleEditClose = useCallback(() => setEditFormOpen(false), [])

  const handleUpload = useCallback(async (file) => {
    setUploading(true)
    try {
      const data = await uploadFile(file, sessionId)
      setPendingFile({ refId: data.ref_id, fileName: data.file_name, fileType: data.file_type, preview: data.preview })
    } catch (err) {
      setMessages(prev => [...prev, {
        id: Date.now(), role: 'agent', agent: 'Data Product Assistant',
        text: `Could not read the file: ${err.message}`, chips: [], loading: false, time: ts(),
      }])
    }
    setUploading(false)
  }, [sessionId])

  const handleUseFile = useCallback((refId) => {
    const pf = pendingFile
    if (!pf) return
    sendMessage('', { action: 'use_file', fileRefId: refId, fileName: pf.fileName })
  }, [pendingFile, sendMessage])

  const handleDismissFile = useCallback(() => setPendingFile(null), [])

  return (
    <div className="w-full h-screen flex flex-col" style={{ background: BP.bg, color: BP.text }}>
      {/* Demo header */}
      <div className="flex items-center justify-between px-5 py-2 shrink-0 border-b" style={{ background: 'white', borderColor: BP.border }}>
        <div className="flex items-center gap-3">
          <div className="flex items-center justify-center" style={{ width: 28, height: 28, borderRadius: 6, background: BP.green, color: 'white', fontSize: 13, fontWeight: 800 }}>
            {'>'}
          </div>
          <div>
            <div className="t-13 font-bold leading-tight" style={{ color: BP.text }}>Data Product Assistant</div>
            <div className="t-105" style={{ color: BP.textMuted }}>Powered by Accenture Agentic Platform</div>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <PhaseStepper activePhase={activePhase} />
          <button onClick={downloadChatPDF} disabled={downloading}
            className="t-115 px-3 py-1 rounded-md font-semibold flex items-center gap-1.5"
            style={{ border: `1px solid ${BP.border}`, color: BP.greenDark, background: 'white', cursor: downloading ? 'default' : 'pointer', opacity: downloading ? 0.6 : 1 }}
            title="Download the conversation as a PDF">
            <Download size={13} /> {downloading ? 'Generating…' : 'Download chat'}
          </button>
          <button onClick={restart}
            className="t-115 px-3 py-1 rounded-md font-semibold flex items-center gap-1.5"
            style={{ border: `1px solid ${BP.border}`, color: BP.textMuted, background: 'white', cursor: 'pointer' }}
            title="Restart the conversation">
            <RotateCcw size={12} /> Restart
          </button>
          <div className="flex items-center gap-2 pl-3" style={{ borderLeft: `1px solid ${BP.border}` }}>
            <div className="flex items-center justify-center" title={user.email}
              style={{ width: 30, height: 30, borderRadius: '50%', background: BP.greenDark, color: 'white', fontSize: 11, fontWeight: 700, letterSpacing: 0.2 }}>
              {user.initials}
            </div>
            <div className="hidden md:block" style={{ lineHeight: 1.15 }}>
              <div className="t-115 font-semibold" style={{ color: BP.text }}>{user.name}</div>
              <div className="t-105" style={{ color: BP.textMuted }}>{user.email}</div>
            </div>
          </div>
        </div>
      </div>

      {/* Main row */}
      <div className="flex" style={{ flex: 1, minHeight: 0 }}>
        <NavRail activePhase={activePhase} />
        <ChatSidebar activePhase={activePhase} />
        <ChatPane
          messages={messages}
          onSend={sendMessage}
          onChipClick={handleChipClick}
          sending={sending}
          inputLocked={!startingPointPicked}
          allowUpload={allowUpload}
          onUpload={handleUpload}
          uploading={uploading}
          pendingFile={pendingFile}
          onUseFile={handleUseFile}
          onDismissFile={handleDismissFile}
          editFormOpen={editFormOpen}
          requirementData={requirementData}
          glossaryData={glossaryData}
          onEditSubmit={handleEditSubmit}
          onEditClose={handleEditClose}
          activeAgent={activeAgent}
          currentActivity={currentActivity}
        />
        <FilesPanel generatedFiles={generatedFiles} onDownload={handleDownload} />
      </div>

      {tweakMode && (
        <TweakModal mode={tweakMode} onSubmit={handleTweakSubmit} onClose={handleTweakClose} />
      )}
    </div>
  )
}
