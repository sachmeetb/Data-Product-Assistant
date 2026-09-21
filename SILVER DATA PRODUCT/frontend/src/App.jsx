import { useState, useCallback } from 'react'
import LeftPane from './components/LeftPane'
import ChatPane from './components/ChatPane'
import RightPane from './components/RightPane'
import TweakModal from './components/TweakModal'
import { sendChatMessage, downloadFile, uploadFile } from './api/chat'

const DDI_CHIP_ADJUST_ER   = 'Adjust the model'
const DDI_CHIP_TWEAK_STTM  = 'Tweak the mapping'

const UPLOAD_ALLOWED_STEPS = new Set([null, 'initial', 'dpi_clarifying', 'dpi_phase_b'])

function ts() {
  return new Date().toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
}

export default function App() {
  const [messages, setMessages]           = useState([{
    id: 0, role: 'agent', agent: 'DATA DOMAIN SILVER AGENT',
    text: "Welcome to **DATA DOMAIN SILVER AGENT**! Please enter your data domain requirements, or upload a file with your requirements.",
    chips: [], loading: false, time: ts(), startingPoint: false,
  }])
  const [sessionId, setSessionId]             = useState(null)
  const [generatedFiles, setGeneratedFiles]   = useState([])
  const [sending, setSending]                 = useState(false)
  const [startingPointPicked, setStartingPointPicked] = useState(true)
  const [currentStep, setCurrentStep]         = useState(null)
  const [uploading, setUploading]             = useState(false)
  const [pendingFile, setPendingFile]         = useState(null)
  const [requirementData, setRequirementData] = useState(null)
  const [glossaryData, setGlossaryData]       = useState(null)
  const [editFormOpen, setEditFormOpen]       = useState(false)
  const [tweakMode, setTweakMode]             = useState(null) // 'er' | 'sttm' | null

  const allowUpload = UPLOAD_ALLOWED_STEPS.has(currentStep) && !pendingFile

  const handleDownload = useCallback((fileId, fileName) => {
    downloadFile(fileId, fileName).catch(err => console.error('Download failed:', err))
  }, [])

  const applyResponse = useCallback((data, thinkId) => {
    if (data.current_step) setCurrentStep(data.current_step)

    if (data.messages?.length) {
      // Extract requirement_data if backend attached it (shown at dpi_confirm_req)
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
          agent: msg.agent || 'DATA DOMAIN SILVER AGENT',
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
          data_contract_view: msg.data_contract_view ?? (idx === data.messages.length - 1 ? data.data_contract_view : undefined),
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
                stage: f.stage || undefined,
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
      { id: thinkId, role: 'agent', agent: 'DATA DOMAIN SILVER AGENT', loading: true, time: ts() },
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

  // Intercept chip clicks — "Edit" / "Let me tweak this" open the form; rest go to backend
  const handleChipClick = useCallback((label) => {
    if ((label === 'Edit' || label === 'Let me tweak this' || label === 'Let me tweak one') && requirementData) {
      setEditFormOpen(true)
      return
    }
    if (label === DDI_CHIP_ADJUST_ER) {
      setTweakMode('er')
      return
    }
    if (label === DDI_CHIP_TWEAK_STTM) {
      setTweakMode('sttm')
      return
    }
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
          // Pipe-separated "Name: new description" pairs — call out that
          // these are data-point description updates so the agent re-emits
          // the matching data_points entries with the new descriptions.
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
      const data = await uploadFile(file)
      setPendingFile({ refId: data.ref_id, fileName: data.file_name, fileType: data.file_type, preview: data.preview })
    } catch (err) {
      setMessages(prev => [...prev, {
        id: Date.now(), role: 'agent', agent: 'DATA DOMAIN SILVER AGENT',
        text: `Could not read the file: ${err.message}`, chips: [], loading: false, time: ts(),
      }])
    }
    setUploading(false)
  }, [])

  const handleUseFile = useCallback((refId) => {
    const pf = pendingFile
    if (!pf) return
    sendMessage('', { action: 'use_file', fileRefId: refId, fileName: pf.fileName })
  }, [pendingFile, sendMessage])

  const handleDismissFile = useCallback(() => setPendingFile(null), [])

  return (
    <div className="app">
      <LeftPane />
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
      />
      <RightPane generatedFiles={generatedFiles} onDownload={handleDownload} />
      {tweakMode && (
        <TweakModal
          mode={tweakMode}
          onSubmit={handleTweakSubmit}
          onClose={handleTweakClose}
        />
      )}
    </div>
  )
}
