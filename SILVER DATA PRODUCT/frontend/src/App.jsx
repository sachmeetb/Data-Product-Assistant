import { useState, useCallback, useRef } from 'react'
import LeftPane from './components/LeftPane'
import ChatPane from './components/ChatPane'
import RightPane from './components/RightPane'
import TweakModal from './components/TweakModal'
import HierarchicalSelectorModal from './components/HierarchicalSelectorModal'
import { sendChatMessage, downloadFile, uploadFile } from './api/chat'
import { DEFAULT_SILVER_CONTRACT } from './components/DataContractCard'

const DDI_CHIP_ADJUST_ER   = 'Adjust the model'
const DDI_CHIP_TWEAK_STTM  = 'Tweak the mapping'

const UPLOAD_ALLOWED_STEPS = new Set([null, 'initial', 'dpi_clarifying', 'dpi_phase_b'])

function ts() {
  return new Date().toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
}

export default function App() {
  const [messages, setMessages]           = useState([{
    id: 0, role: 'agent', agent: 'Silver Data Product Assistant',
    text: "Welcome to **Silver Data Product Assistant**! To get started, please select your target **Industry** to explore available canonical domain models, or click **Domain Navigator** to drill down into entities and attributes.",
    chips: [
      "Banking & Financial Services",
      "Map Bronze → Silver Customer (4-to-1 STTM)",
      "Industrial & Water (Ecolab)",
      "Retail & Commerce",
      "Automotive & Fleet",
      "Life Sciences",
    ],
    loading: false, time: ts(), startingPoint: false,
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
  const [navigatorOpen, setNavigatorOpen]     = useState(false)
  const latestContractRef                     = useRef(null)
  const latestGlossaryRef                     = useRef(null)

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
      if (gloss) {
        setGlossaryData(gloss)
        latestGlossaryRef.current = gloss
      }

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
          entity_selector_view: msg.entity_selector_view
            ?? (idx === data.messages.length - 1 ? data.entity_selector_view : undefined),
          glossary: msg.glossary ?? undefined,
          classification_view: msg.classification_view ?? undefined,
          challenger_view: msg.challenger_view ?? undefined,
          sttm_view: msg.sttm_view ?? undefined,
          silver_transform_view: msg.silver_transform_view ?? undefined,
          data_contract_view: msg.data_contract_view ?? (idx === data.messages.length - 1 ? data.data_contract_view : undefined),
          publish_view: msg.publish_view ?? (idx === data.messages.length - 1 ? data.publish_view : undefined),
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
              entity_selector_view: data.entity_selector_view,
              classification_view: data.classification_view,
              publish_view: data.publish_view,
              conformance_view: data.conformance_view }
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
        contract: opts.contract,
        glossary: opts.glossary,
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

  const handleContractChange = useCallback((updatedContract) => {
    latestContractRef.current = updatedContract
  }, [])

  const handleSaveContract = useCallback((finalizedContract) => {
    const contractToSave = finalizedContract || latestContractRef.current || DEFAULT_SILVER_CONTRACT
    latestContractRef.current = contractToSave
    sendMessage('Apply & Save Contract', {
      action: 'save_contract',
      contract: contractToSave,
    })
  }, [sendMessage])

  const handleGlossaryChange = useCallback((updatedGlossary) => {
    latestGlossaryRef.current = updatedGlossary
  }, [])

  const handleSaveGlossary = useCallback((finalizedGlossary) => {
    const glossToSave = finalizedGlossary || latestGlossaryRef.current
    if (glossToSave) {
      latestGlossaryRef.current = glossToSave
      setGlossaryData(glossToSave)
    }
  }, [])

  const handleConfirmWithGlossary = useCallback((finalizedGlossary) => {
    const glossToSave = finalizedGlossary || latestGlossaryRef.current
    if (glossToSave) {
      latestGlossaryRef.current = glossToSave
      setGlossaryData(glossToSave)
    }
    sendMessage('Confirm Requirement', {
      action: 'confirm_req',
      glossary: glossToSave,
    })
  }, [sendMessage])

  // Intercept chip clicks — "Edit" / "Let me tweak this" open the form; rest go to backend
  const handleChipClick = useCallback((label) => {
    if ((label === 'Edit' || label === 'Let me tweak this' || label === 'Let me tweak one') && requirementData) {
      setEditFormOpen(true)
      return
    }
    if (label === 'Apply & Save Contract' || label === 'Save Contract') {
      handleSaveContract(latestContractRef.current)
      return
    }
    if (label === 'Publish to BigQuery' || label === 'Publish') {
      sendMessage('Publish to BigQuery', { action: 'publish_bq' })
      return
    }
    if (label === 'Confirm Requirement' || label === 'Design Silver Schema') {
      sendMessage(label, { glossary: latestGlossaryRef.current })
      return
    }
    if (label === 'Open & Edit Existing Silver Structure') {
      sendMessage(label, { action: 'open_existing_structure', glossary: latestGlossaryRef.current })
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
  }, [requirementData, sendMessage, handleSaveContract])

  const handleApplyEntityScope = useCallback(({ selectedEntities, mode, domainLabel }) => {
    if (!selectedEntities || selectedEntities.length === 0) return
    const prompt = mode === 'all'
      ? `Select All Entities (${selectedEntities.length})`
      : mode === 'single'
        ? `Select Only ${selectedEntities[0]}`
        : `Proceed with selected entities for ${domainLabel || 'domain'}: ${selectedEntities.join(', ')}`
    sendMessage(prompt, { action: 'select_entities' })
  }, [sendMessage])

  const handleOpenExistingStructure = useCallback(({ selectedEntities, domainName, domainLabel } = {}) => {
    const entityNote = selectedEntities?.length
      ? ` with entities: ${selectedEntities.join(', ')}`
      : ''
    const prompt = `Open and edit existing Silver structure for ${domainLabel || domainName || 'Core Banking & Accounts'}${entityNote}`
    sendMessage(prompt, { action: 'open_existing_structure', glossary: latestGlossaryRef.current })
  }, [sendMessage])

  const handleTweakSubmit = useCallback((text) => {
    const mode = tweakMode
    setTweakMode(null)
    if (!mode || !text?.trim()) return
    sendMessage(text, { action: mode === 'sttm' ? 'tweak_sttm' : 'tweak_er' })
  }, [tweakMode, sendMessage])

  const handleTweakClose = useCallback(() => setTweakMode(null), [])

  const handleEditSubmit = useCallback((updates, updatedGlossary) => {
    setEditFormOpen(false)
    if (updatedGlossary) {
      latestGlossaryRef.current = updatedGlossary
      setGlossaryData(updatedGlossary)
    }
    const lines = Object.entries(updates)
      .filter(([, v]) => v !== null && v !== undefined && String(v).trim() !== '')
      .map(([k, v]) => {
        if (k === 'Glossary Definitions') {
          return `- Update these data point descriptions (one per pipe): ${v}`
        }
        return `- ${k}: ${v}`
      })
    const msg = `Please update my requirement with the following changes:\n${lines.join('\n')}`
    sendMessage(msg, { action: 'edit', glossary: updatedGlossary || latestGlossaryRef.current })
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
        onOpenNavigator={() => setNavigatorOpen(true)}
        sessionId={sessionId}
        onApplyEntityScope={handleApplyEntityScope}
        onOpenExistingStructure={handleOpenExistingStructure}
        onSaveContract={handleSaveContract}
        onContractChange={handleContractChange}
        onGlossaryChange={handleGlossaryChange}
        onSaveGlossary={handleSaveGlossary}
        onConfirmWithGlossary={handleConfirmWithGlossary}
      />
      <RightPane generatedFiles={generatedFiles} onDownload={handleDownload} />
      {tweakMode && (
        <TweakModal
          mode={tweakMode}
          onSubmit={handleTweakSubmit}
          onClose={handleTweakClose}
        />
      )}
      <HierarchicalSelectorModal
        isOpen={navigatorOpen}
        onClose={() => setNavigatorOpen(false)}
        sessionId={sessionId}
        onApply={(text) => sendMessage(text)}
        onOpenExistingStructure={(payload) => {
          handleOpenExistingStructure({
            selectedEntities: payload?.selected_entities,
            domainName: payload?.domain_name,
          })
        }}
      />
    </div>
  )
}
