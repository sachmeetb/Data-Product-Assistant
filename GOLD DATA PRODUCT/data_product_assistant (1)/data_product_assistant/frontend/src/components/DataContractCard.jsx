import { useState, useEffect } from 'react'

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

const SaveIcon = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" />
    <polyline points="17 21 17 13 7 13 7 21" />
    <polyline points="7 3 7 8 15 8" />
  </svg>
)

const PlusIcon = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
    <line x1="12" y1="5" x2="12" y2="19" />
    <line x1="5" y1="12" x2="19" y2="12" />
  </svg>
)

const TrashIcon = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <polyline points="3 6 5 6 21 6" />
    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
  </svg>
)

const DownloadIcon = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
    <polyline points="7 10 12 15 17 10" />
    <line x1="12" y1="15" x2="12" y2="3" />
  </svg>
)

export const DEFAULT_GOLD_CONTRACT = {
  dataContractSpecification: '0.9.3',
  id: 'urn:datacontract:gold:consumption',
  info: {
    title: 'Gold Layer Conformed Data Contract',
    version: '1.0.0',
    status: 'ACTIVE',
    description: 'Official Gold Layer Data Contract governing conformed entities, consumption-ready star/snowflake schemas, KPIs, and automated quality assertions.',
    domain: 'Enterprise Gold Domain',
    owner: 'Enterprise Data Governance & BI Team',
    standards: ['BigQuery Native', 'Knowledge Catalog', 'BCBS 239'],
    target_dataset: 'gold',
  },
  servicelevels: {
    freshness: {
      schedule: 'DAILY_BATCH',
      maxLag: '24h lag',
      cron: '0 4 * * *',
    },
    availability: {
      percentage: '99.9%',
    },
    retention: {
      period: '7 years',
      policy: 'Compliant Archival',
    },
  },
  models: {
    gold_dim_customer: {
      description: 'Conformed Customer Master Dimension',
      type: 'table',
      physicalName: 'gold.dim_customer',
      fields: {
        customer_key: { type: 'string', description: 'Surrogate primary key for customer', primary: true, nullable: false },
        customer_id: { type: 'string', description: 'Enterprise customer master identifier', primary: false, nullable: false },
        customer_name: { type: 'string', description: 'Full legal customer name', pii: true, classification: 'PII', nullable: false },
        segment: { type: 'string', description: 'Business segment classification', nullable: false },
        country_code: { type: 'string', description: 'ISO 3166-1 alpha-2 country code', nullable: false },
        effective_from: { type: 'timestamp', description: 'SCD Type 2 valid start timestamp', nullable: false },
        effective_to: { type: 'timestamp', description: 'SCD Type 2 valid end timestamp', nullable: true },
      },
    },
    gold_fact_transactions: {
      description: 'Conformed Transaction Performance Fact Table',
      type: 'table',
      physicalName: 'gold.fact_transactions',
      fields: {
        fact_key: { type: 'string', description: 'Surrogate transaction fact key', primary: true, nullable: false },
        transaction_id: { type: 'string', description: 'Business transaction posting identifier', primary: false, nullable: false },
        customer_key: { type: 'string', description: 'Foreign key to dim_customer', references: 'gold.dim_customer.customer_key', nullable: false },
        total_amount: { type: 'numeric', description: 'Monetary transaction posting amount', nullable: false },
        currency_code: { type: 'string', description: 'ISO 4217 currency code', nullable: false },
        transaction_date: { type: 'date', description: 'Calendar date of transaction posting', nullable: false },
      },
    },
  },
  quality: [
    {
      type: 'custom',
      description: 'Surrogate primary key must be unique and non-null',
      mustBe: 'customer_key IS NOT NULL',
      severity: 'CRITICAL',
    },
    {
      type: 'custom',
      description: 'Foreign key customer_key must reference valid conformed dimension',
      mustBe: 'customer_key IN (SELECT customer_key FROM gold.dim_customer)',
      severity: 'CRITICAL',
    },
    {
      type: 'custom',
      description: 'Currency code must strictly follow ISO 4217 3-letter uppercase standard',
      mustBe: "currency_code MATCHES '^[A-Z]{3}$'",
      severity: 'HIGH',
    },
    {
      type: 'custom',
      description: 'Total transaction amount must be strictly positive',
      mustBe: 'total_amount > 0',
      severity: 'CRITICAL',
    },
  ],
}

export const DEFAULT_SILVER_CONTRACT = DEFAULT_GOLD_CONTRACT

function generateYamlString(data) {
  const lines = []
  lines.push(`dataContractSpecification: "${data.dataContractSpecification || '0.9.3'}"`)
  lines.push(`id: "${data.id || 'urn:datacontract:gold'}"`)
  lines.push('info:')
  lines.push(`  title: "${data.info?.title || ''}"`)
  lines.push(`  version: "${data.info?.version || '1.0.0'}"`)
  lines.push(`  status: "${data.info?.status || 'ACTIVE'}"`)
  lines.push(`  description: "${(data.info?.description || '').replace(/"/g, '\\"')}"`)
  lines.push(`  domain: "${data.info?.domain || ''}"`)
  lines.push(`  owner: "${data.info?.owner || ''}"`)
  lines.push(`  target_dataset: "${data.info?.target_dataset || 'gold'}"`)
  lines.push('  standards:')
  ;(data.info?.standards || []).forEach(s => {
    lines.push(`    - "${s}"`)
  })

  lines.push('servicelevels:')
  lines.push('  freshness:')
  lines.push(`    schedule: "${data.servicelevels?.freshness?.schedule || 'DAILY_BATCH'}"`)
  lines.push(`    maxLag: "${data.servicelevels?.freshness?.maxLag || '24h lag'}"`)
  lines.push(`    cron: "${data.servicelevels?.freshness?.cron || '0 4 * * *'}"`)
  lines.push('  availability:')
  lines.push(`    percentage: "${data.servicelevels?.availability?.percentage || '99.9%'}"`)
  lines.push('  retention:')
  lines.push(`    period: "${data.servicelevels?.retention?.period || '7 years'}"`)
  lines.push(`    policy: "${data.servicelevels?.retention?.policy || 'Compliant Archival'}"`)

  lines.push('models:')
  Object.entries(data.models || {}).forEach(([mName, mDef]) => {
    lines.push(`  ${mName}:`)
    lines.push(`    description: "${(mDef.description || '').replace(/"/g, '\\"')}"`)
    lines.push(`    type: "${mDef.type || 'table'}"`)
    lines.push(`    physicalName: "${mDef.physicalName || mName}"`)
    lines.push('    fields:')
    Object.entries(mDef.fields || {}).forEach(([fName, fDef]) => {
      lines.push(`      ${fName}:`)
      lines.push(`        type: "${fDef.type || 'string'}"`)
      lines.push(`        description: "${(fDef.description || '').replace(/"/g, '\\"')}"`)
      lines.push(`        nullable: ${fDef.nullable ?? true}`)
      if (fDef.primary) lines.push('        primary: true')
      if (fDef.references) lines.push(`        references: "${fDef.references}"`)
      if (fDef.pii) {
        lines.push('        pii: true')
        lines.push('        classification: "PII"')
      }
    })
  })

  lines.push('quality:')
  ;(data.quality || []).forEach(q => {
    lines.push(`  - type: "${q.type || 'custom'}"`)
    lines.push(`    description: "${(q.description || '').replace(/"/g, '\\"')}"`)
    lines.push(`    mustBe: "${(q.mustBe || '').replace(/"/g, '\\"')}"`)
    if (q.severity) lines.push(`    severity: "${q.severity}"`)
  })

  return lines.join('\n')
}

export default function DataContractCard({ view, onSave, isModal, onClose }) {
  const [activeTab, setActiveTab] = useState('schema')
  const [copied, setCopied] = useState(false)
  const [savedBadge, setSavedBadge] = useState(false)
  const [newStandardInput, setNewStandardInput] = useState('')

  // Initialize editable state with full prefilled values
  const [contract, setContract] = useState(() => {
    const base = view && typeof view === 'object' && Object.keys(view).length > 0
      ? JSON.parse(JSON.stringify(view))
      : JSON.parse(JSON.stringify(DEFAULT_GOLD_CONTRACT))

    if (!base.info) base.info = {}
    if (!base.servicelevels) base.servicelevels = { freshness: {}, availability: {}, retention: {} }
    if (!base.models || Object.keys(base.models).length === 0) base.models = DEFAULT_GOLD_CONTRACT.models
    if (!base.quality || base.quality.length === 0) base.quality = DEFAULT_GOLD_CONTRACT.quality

    // Fill in default placeholders if any empty string exists
    if (!base.info.title) base.info.title = DEFAULT_GOLD_CONTRACT.info.title
    if (!base.info.owner) base.info.owner = DEFAULT_GOLD_CONTRACT.info.owner
    if (!base.info.standards || base.info.standards.length === 0) base.info.standards = DEFAULT_GOLD_CONTRACT.info.standards
    if (!base.info.target_dataset) base.info.target_dataset = DEFAULT_GOLD_CONTRACT.info.target_dataset
    if (!base.servicelevels.freshness.schedule) base.servicelevels.freshness.schedule = 'DAILY_BATCH'
    if (!base.servicelevels.freshness.maxLag) base.servicelevels.freshness.maxLag = '24h lag'

    return base
  })

  // Whenever view changes externally, update contract if provided
  useEffect(() => {
    if (view && typeof view === 'object' && Object.keys(view).length > 0) {
      setContract(prev => ({
        ...DEFAULT_GOLD_CONTRACT,
        ...view,
        info: { ...DEFAULT_GOLD_CONTRACT.info, ...(view.info || {}) },
        servicelevels: { ...DEFAULT_GOLD_CONTRACT.servicelevels, ...(view.servicelevels || {}) },
        models: (view.models && Object.keys(view.models).length > 0) ? view.models : DEFAULT_GOLD_CONTRACT.models,
        quality: (view.quality && view.quality.length > 0) ? view.quality : DEFAULT_GOLD_CONTRACT.quality,
      }))
    }
  }, [view])

  // Overview field mutator
  const updateInfo = (field, val) => {
    setContract(prev => ({
      ...prev,
      info: { ...prev.info, [field]: val },
    }))
  }

  // Service Level mutator
  const updateSla = (section, field, val) => {
    setContract(prev => ({
      ...prev,
      servicelevels: {
        ...prev.servicelevels,
        [section]: {
          ...(prev.servicelevels[section] || {}),
          [field]: val,
        },
      },
    }))
  }

  // Standards tag management
  const addStandard = () => {
    if (!newStandardInput.trim()) return
    const std = newStandardInput.trim()
    setContract(prev => ({
      ...prev,
      info: {
        ...prev.info,
        standards: [...(prev.info.standards || []), std],
      },
    }))
    setNewStandardInput('')
  }

  const removeStandard = (idx) => {
    setContract(prev => ({
      ...prev,
      info: {
        ...prev.info,
        standards: prev.info.standards.filter((_, i) => i !== idx),
      },
    }))
  }

  // Model & Field mutators
  const updateModelDef = (modelName, prop, val) => {
    setContract(prev => ({
      ...prev,
      models: {
        ...prev.models,
        [modelName]: {
          ...prev.models[modelName],
          [prop]: val,
        },
      },
    }))
  }

  const updateFieldDef = (modelName, fieldName, prop, val) => {
    setContract(prev => {
      const curFields = { ...(prev.models[modelName]?.fields || {}) }
      curFields[fieldName] = { ...curFields[fieldName], [prop]: val }
      return {
        ...prev,
        models: {
          ...prev.models,
          [modelName]: {
            ...prev.models[modelName],
            fields: curFields,
          },
        },
      }
    })
  }

  const renameFieldName = (modelName, oldName, newName) => {
    if (!newName.trim() || oldName === newName) return
    setContract(prev => {
      const curFields = { ...(prev.models[modelName]?.fields || {}) }
      const fieldObj = curFields[oldName] || { type: 'string', description: '' }
      delete curFields[oldName]
      curFields[newName] = fieldObj
      return {
        ...prev,
        models: {
          ...prev.models,
          [modelName]: {
            ...prev.models[modelName],
            fields: curFields,
          },
        },
      }
    })
  }

  const addFieldToModel = (modelName) => {
    const colName = `new_column_${Date.now().toString().slice(-4)}`
    setContract(prev => {
      const curFields = { ...(prev.models[modelName]?.fields || {}) }
      curFields[colName] = {
        type: 'string',
        description: 'New conformed column',
        nullable: true,
        primary: false,
        pii: false,
      }
      return {
        ...prev,
        models: {
          ...prev.models,
          [modelName]: {
            ...prev.models[modelName],
            fields: curFields,
          },
        },
      }
    })
  }

  const deleteFieldFromModel = (modelName, fieldName) => {
    setContract(prev => {
      const curFields = { ...(prev.models[modelName]?.fields || {}) }
      delete curFields[fieldName]
      return {
        ...prev,
        models: {
          ...prev.models,
          [modelName]: {
            ...prev.models[modelName],
            fields: curFields,
          },
        },
      }
    })
  }

  // Quality Rule mutators
  const updateQualityRule = (idx, prop, val) => {
    setContract(prev => {
      const rules = [...(prev.quality || [])]
      rules[idx] = { ...rules[idx], [prop]: val }
      return { ...prev, quality: rules }
    })
  }

  const addQualityRule = () => {
    setContract(prev => ({
      ...prev,
      quality: [
        ...(prev.quality || []),
        {
          type: 'custom',
          description: 'New automated quality validation constraint',
          mustBe: 'column_name IS NOT NULL',
          severity: 'HIGH',
        },
      ],
    }))
  }

  const deleteQualityRule = (idx) => {
    setContract(prev => ({
      ...prev,
      quality: prev.quality.filter((_, i) => i !== idx),
    }))
  }

  // Save Contract
  const handleSave = () => {
    const yamlString = generateYamlString(contract)
    const finalized = { ...contract, yaml_text: yamlString }
    if (onSave) onSave(finalized)
    setSavedBadge(true)
    setTimeout(() => setSavedBadge(false), 3000)
  }

  const handleReset = () => {
    setContract(JSON.parse(JSON.stringify(DEFAULT_GOLD_CONTRACT)))
    setSavedBadge(true)
    setTimeout(() => setSavedBadge(false), 2000)
  }

  const generatedYaml = generateYamlString(contract)

  const handleCopy = () => {
    navigator.clipboard.writeText(generatedYaml)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const handleDownloadYaml = () => {
    const blob = new Blob([generatedYaml], { type: 'text/yaml;charset=utf-8;' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${contract.info?.title?.toLowerCase().replace(/\s+/g, '_') || 'gold_contract'}.yaml`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  const inputStyle = {
    background: '#ffffff',
    border: '1px solid #d1d5db',
    borderRadius: 5,
    padding: '5px 8px',
    fontSize: 11,
    fontFamily: 'Inter, system-ui, sans-serif',
    color: '#111827',
    width: '100%',
    boxSizing: 'border-box',
    outline: 'none',
    transition: 'border-color 0.15s',
  }

  return (
    <div style={{
      border: '1px solid #7500C0',
      borderRadius: 10,
      margin: '12px 0',
      overflow: 'hidden',
      background: '#ffffff',
      fontSize: 12,
      fontFamily: 'Inter, system-ui, -apple-system, sans-serif',
      boxShadow: '0 4px 14px rgba(117, 0, 192, 0.12)',
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
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <div style={{
            background: 'rgba(255, 255, 255, 0.2)',
            borderRadius: 6,
            padding: '5px 8px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}>
            <FileCodeIcon />
          </div>
          <div>
            <div style={{ fontSize: 13.5, fontWeight: 700, letterSpacing: '0.01em', display: 'flex', alignItems: 'center', gap: 8 }}>
              <span>{contract.info?.title || 'Gold Layer Data Contract'}</span>
              <span style={{
                background: '#A100FF',
                color: '#ffffff',
                fontSize: 9.5,
                padding: '1px 7px',
                borderRadius: 10,
                fontWeight: 700,
                border: '1px solid #C2A3FF',
              }}>
                ✎ Editable Mode
              </span>
            </div>
            <div style={{ fontSize: 10.5, color: '#E6DCFF', fontFamily: 'monospace', marginTop: 2 }}>
              {contract.id || 'urn:datacontract:gold'}
            </div>
          </div>
        </div>

        {/* Header Right Actions */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {savedBadge && (
            <span style={{
              background: '#10B981',
              color: '#ffffff',
              fontSize: 10.5,
              fontWeight: 700,
              padding: '3px 8px',
              borderRadius: 6,
              display: 'flex',
              alignItems: 'center',
              gap: 4,
              boxShadow: '0 1px 4px rgba(0,0,0,0.15)',
            }}>
              <CheckIcon /> Contract Saved!
            </span>
          )}

          <button
            onClick={handleSave}
            title="Save your changes to this Data Contract"
            style={{
              background: '#10B981',
              color: '#ffffff',
              border: 'none',
              borderRadius: 6,
              padding: '6px 12px',
              fontSize: 11,
              fontWeight: 700,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: 5,
              boxShadow: '0 1px 3px rgba(0,0,0,0.2)',
              transition: 'background 0.15s',
            }}
          >
            <SaveIcon /> Save Contract
          </button>

          <button
            onClick={handleReset}
            title="Reset contract fields back to default baseline"
            style={{
              background: 'rgba(255, 255, 255, 0.18)',
              color: '#ffffff',
              border: '1px solid rgba(255, 255, 255, 0.35)',
              borderRadius: 6,
              padding: '6px 10px',
              fontSize: 11,
              fontWeight: 600,
              cursor: 'pointer',
            }}
          >
            Reset
          </button>

          {isModal && onClose && (
            <button
              onClick={onClose}
              style={{
                background: 'rgba(255, 255, 255, 0.25)',
                color: '#fff',
                border: 'none',
                borderRadius: '50%',
                width: 24,
                height: 24,
                cursor: 'pointer',
                fontWeight: 700,
                fontSize: 13,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
              }}
            >
              ✕
            </button>
          )}
        </div>
      </div>

      {/* ── Instruction Subheader Banner ── */}
      <div style={{
        background: '#FAF5FF',
        borderBottom: '1px solid #E9D5FF',
        padding: '6px 14px',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        fontSize: 11,
        color: '#6B21A8',
      }}>
        <span>
          💡 <strong>Values are pre-filled below:</strong> Click into any text or attribute to edit or delete the previous value.
        </span>
        <span style={{ fontSize: 10.5, color: '#7E22CE', fontWeight: 600 }}>
          {Object.keys(contract.models || {}).length} Models · {contract.quality?.length || 0} Quality Rules
        </span>
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
          { id: 'schema', label: `Models & Columns (${Object.keys(contract.models || {}).length})` },
          { id: 'quality', label: `Quality Rules (${contract.quality?.length || 0})` },
          { id: 'overview', label: 'Overview & SLAs' },
          { id: 'yaml', label: 'Live YAML Spec' },
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
              padding: '8px 14px',
              fontSize: 11.5,
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

      {/* ── Tab Contents ── */}
      <div style={{ padding: 14 }}>
        {/* =================================================================== */}
        {/* TAB 1: MODELS & COLUMNS (EDITABLE)                                  */}
        {/* =================================================================== */}
        {activeTab === 'schema' && (
          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
              <div style={{ fontSize: 11.5, color: '#4b5563' }}>
                Conformed entities and attributes. Edit column names, types, flags, or add/delete attributes:
              </div>
              <button
                type="button"
                onClick={() => {
                  const mKey = `gold_new_entity_${Date.now().toString().slice(-4)}`
                  setContract(prev => ({
                    ...prev,
                    models: {
                      ...prev.models,
                      [mKey]: {
                        description: 'New Conformed Entity Table',
                        type: 'table',
                        physicalName: `gold.${mKey}`,
                        fields: {
                          surrogate_key: { type: 'string', description: 'Surrogate Primary Key', primary: true, nullable: false },
                          id: { type: 'string', description: 'Business Identifier', primary: false, nullable: false },
                        },
                      },
                    },
                  }))
                }}
                style={{
                  background: '#E6DCFF',
                  color: '#460073',
                  border: '1px solid #C2A3FF',
                  borderRadius: 6,
                  padding: '4px 10px',
                  fontSize: 11,
                  fontWeight: 700,
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 4,
                }}
              >
                <PlusIcon /> Add Entity Table
              </button>
            </div>

            {Object.entries(contract.models || {}).map(([modelName, modelDef]) => (
              <div key={modelName} style={{ marginBottom: 16, border: '1px solid #e5e7eb', borderRadius: 8, overflow: 'hidden', background: '#fafafa' }}>
                {/* Model Header */}
                <div style={{
                  background: '#f1f5f9',
                  padding: '8px 12px',
                  borderBottom: '1px solid #e2e8f0',
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  flexWrap: 'wrap',
                  gap: 8,
                }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, flex: 1, minWidth: 260 }}>
                    <span style={{ fontSize: 10.5, fontWeight: 700, color: '#460073', textTransform: 'uppercase' }}>Table:</span>
                    <input
                      type="text"
                      value={modelDef.physicalName || modelName}
                      onChange={e => updateModelDef(modelName, 'physicalName', e.target.value)}
                      style={{ ...inputStyle, width: 'auto', minWidth: 200, fontFamily: 'monospace', fontWeight: 700, color: '#460073' }}
                      title="Delete previous value and type physical table name"
                    />
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, flex: 2, minWidth: 260 }}>
                    <span style={{ fontSize: 10.5, fontWeight: 600, color: '#64748b' }}>Desc:</span>
                    <input
                      type="text"
                      value={modelDef.description || ''}
                      onChange={e => updateModelDef(modelName, 'description', e.target.value)}
                      style={{ ...inputStyle }}
                      placeholder="Table description..."
                    />
                    <button
                      type="button"
                      onClick={() => addFieldToModel(modelName)}
                      style={{
                        background: '#7500C0',
                        color: '#fff',
                        border: 'none',
                        borderRadius: 5,
                        padding: '4px 9px',
                        fontSize: 10.5,
                        fontWeight: 700,
                        cursor: 'pointer',
                        whiteSpace: 'nowrap',
                        display: 'flex',
                        alignItems: 'center',
                        gap: 3,
                      }}
                      title="Add new column to this table"
                    >
                      <PlusIcon /> Add Column
                    </button>
                  </div>
                </div>

                {/* Fields Table */}
                <div style={{ overflowX: 'auto', background: '#ffffff' }}>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
                    <thead>
                      <tr style={{ background: '#f8fafc', color: '#475569', textAlign: 'left', borderBottom: '1px solid #e2e8f0' }}>
                        <th style={{ padding: '6px 10px', width: '22%' }}>Field Name</th>
                        <th style={{ padding: '6px 10px', width: '16%' }}>Data Type</th>
                        <th style={{ padding: '6px 10px', width: '22%' }}>Flags (PK / FK / PII)</th>
                        <th style={{ padding: '6px 10px', width: '34%' }}>Business Description</th>
                        <th style={{ padding: '6px 8px', width: '6%', textAlign: 'center' }}>Action</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(modelDef.fields || {}).map(([fName, fDef]) => (
                        <tr key={fName} style={{ borderBottom: '1px solid #f1f5f9' }}>
                          {/* Column Name Input */}
                          <td style={{ padding: '6px 10px' }}>
                            <input
                              type="text"
                              value={fName}
                              onChange={e => renameFieldName(modelName, fName, e.target.value)}
                              style={{ ...inputStyle, fontFamily: 'monospace', fontWeight: 600, color: '#0f172a' }}
                              title="Delete previous value to rename column"
                            />
                          </td>

                          {/* Data Type Input */}
                          <td style={{ padding: '6px 10px' }}>
                            <select
                              value={fDef.type || 'string'}
                              onChange={e => updateFieldDef(modelName, fName, 'type', e.target.value)}
                              style={{ ...inputStyle, fontFamily: 'monospace', color: '#7500C0', fontWeight: 600, cursor: 'pointer' }}
                            >
                              <option value="string">string</option>
                              <option value="numeric">numeric</option>
                              <option value="timestamp">timestamp</option>
                              <option value="date">date</option>
                              <option value="boolean">boolean</option>
                              <option value="int64">int64</option>
                              <option value="array">array</option>
                              <option value="struct">struct</option>
                            </select>
                          </td>

                          {/* Flags: PK, FK, PII */}
                          <td style={{ padding: '6px 10px' }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                              <label style={{ display: 'flex', alignItems: 'center', gap: 3, cursor: 'pointer', fontSize: 10, fontWeight: 700, color: '#460073' }}>
                                <input
                                  type="checkbox"
                                  checked={!!fDef.primary}
                                  onChange={e => updateFieldDef(modelName, fName, 'primary', e.target.checked)}
                                />
                                PK
                              </label>

                              <label style={{ display: 'flex', alignItems: 'center', gap: 3, cursor: 'pointer', fontSize: 10, fontWeight: 700, color: '#dc2626' }}>
                                <input
                                  type="checkbox"
                                  checked={!!fDef.pii}
                                  onChange={e => updateFieldDef(modelName, fName, 'pii', e.target.checked)}
                                />
                                PII
                              </label>

                              <div style={{ display: 'flex', alignItems: 'center', gap: 2, flex: 1, minWidth: 80 }}>
                                <span style={{ fontSize: 9.5, color: '#0284c7', fontWeight: 600 }}>FK:</span>
                                <input
                                  type="text"
                                  value={fDef.references || ''}
                                  placeholder="ref.col"
                                  onChange={e => updateFieldDef(modelName, fName, 'references', e.target.value)}
                                  style={{ ...inputStyle, padding: '2px 4px', fontSize: 10, fontFamily: 'monospace' }}
                                  title="Foreign key reference (e.g. gold.dim_customer.customer_key)"
                                />
                              </div>
                            </div>
                          </td>

                          {/* Description Input */}
                          <td style={{ padding: '6px 10px' }}>
                            <input
                              type="text"
                              value={fDef.description || ''}
                              onChange={e => updateFieldDef(modelName, fName, 'description', e.target.value)}
                              style={{ ...inputStyle, color: '#475569' }}
                              placeholder="Column business description..."
                            />
                          </td>

                          {/* Delete Column */}
                          <td style={{ padding: '6px 8px', textAlign: 'center' }}>
                            <button
                              type="button"
                              onClick={() => deleteFieldFromModel(modelName, fName)}
                              title={`Delete column ${fName}`}
                              style={{
                                background: '#fee2e2',
                                color: '#dc2626',
                                border: '1px solid #fca5a5',
                                borderRadius: 4,
                                padding: '3px 6px',
                                cursor: 'pointer',
                                display: 'inline-flex',
                                alignItems: 'center',
                                justifyContent: 'center',
                              }}
                            >
                              <TrashIcon />
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ))}
          </div>
        )}

        {/* =================================================================== */}
        {/* TAB 2: QUALITY RULES (EDITABLE)                                    */}
        {/* =================================================================== */}
        {activeTab === 'quality' && (
          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
              <div style={{ fontSize: 11.5, color: '#4b5563' }}>
                Automated data quality assertion rules. Edit rule descriptions, assertion expressions, or add/delete rules:
              </div>
              <button
                type="button"
                onClick={addQualityRule}
                style={{
                  background: '#7500C0',
                  color: '#fff',
                  border: 'none',
                  borderRadius: 6,
                  padding: '5px 12px',
                  fontSize: 11,
                  fontWeight: 700,
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 4,
                }}
              >
                <PlusIcon /> Add Quality Rule
              </button>
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {(contract.quality || []).map((q, idx) => (
                <div key={idx} style={{
                  background: '#f8fafc',
                  border: '1px solid #e2e8f0',
                  borderLeft: `4px solid ${q.severity === 'CRITICAL' ? '#dc2626' : q.severity === 'HIGH' ? '#7500C0' : '#0284c7'}`,
                  borderRadius: 6,
                  padding: 12,
                }}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10, marginBottom: 8 }}>
                    <div style={{ flex: 1 }}>
                      <span style={{ fontSize: 10, fontWeight: 700, color: '#64748b', textTransform: 'uppercase' }}>Rule Description:</span>
                      <input
                        type="text"
                        value={q.description || ''}
                        onChange={e => updateQualityRule(idx, 'description', e.target.value)}
                        style={{ ...inputStyle, fontWeight: 600, color: '#0f172a', marginTop: 2 }}
                        placeholder="Rule description..."
                      />
                    </div>
                    <div style={{ width: 120 }}>
                      <span style={{ fontSize: 10, fontWeight: 700, color: '#64748b', textTransform: 'uppercase' }}>Severity:</span>
                      <select
                        value={q.severity || 'HIGH'}
                        onChange={e => updateQualityRule(idx, 'severity', e.target.value)}
                        style={{ ...inputStyle, fontWeight: 700, marginTop: 2, cursor: 'pointer' }}
                      >
                        <option value="CRITICAL">CRITICAL</option>
                        <option value="HIGH">HIGH</option>
                        <option value="MEDIUM">MEDIUM</option>
                      </select>
                    </div>
                    <button
                      type="button"
                      onClick={() => deleteQualityRule(idx)}
                      title="Delete this quality rule"
                      style={{
                        background: '#fee2e2',
                        color: '#dc2626',
                        border: '1px solid #fca5a5',
                        borderRadius: 4,
                        padding: '6px 8px',
                        cursor: 'pointer',
                        marginTop: 14,
                      }}
                    >
                      <TrashIcon />
                    </button>
                  </div>

                  <div>
                    <span style={{ fontSize: 10, fontWeight: 700, color: '#460073', textTransform: 'uppercase' }}>Executable Assertion Expression (`mustBe`):</span>
                    <input
                      type="text"
                      value={q.mustBe || ''}
                      onChange={e => updateQualityRule(idx, 'mustBe', e.target.value)}
                      style={{
                        ...inputStyle,
                        fontFamily: 'monospace',
                        color: '#460073',
                        background: '#FAF5FF',
                        border: '1px solid #D8B4FE',
                        fontWeight: 600,
                        marginTop: 2,
                      }}
                      placeholder="SQL or Assertion expression (e.g. column_name IS NOT NULL)..."
                    />
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* =================================================================== */}
        {/* TAB 3: OVERVIEW & SLAS (EDITABLE)                                   */}
        {/* =================================================================== */}
        {activeTab === 'overview' && (
          <div>
            <div style={{ marginBottom: 14 }}>
              <span style={{ fontSize: 10.5, fontWeight: 700, color: '#374151', textTransform: 'uppercase' }}>Contract Title & Summary:</span>
              <input
                type="text"
                value={contract.info?.title || ''}
                onChange={e => updateInfo('title', e.target.value)}
                style={{ ...inputStyle, fontWeight: 700, fontSize: 13, color: '#460073', marginTop: 4, marginBottom: 8 }}
                placeholder="Contract Title..."
              />
              <textarea
                rows={2}
                value={contract.info?.description || ''}
                onChange={e => updateInfo('description', e.target.value)}
                style={{ ...inputStyle, resize: 'vertical' }}
                placeholder="Contract purpose and business domain scope description..."
              />
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))', gap: 12, marginBottom: 16 }}>
              <div style={{ background: '#f9fafb', border: '1px solid #e5e7eb', borderRadius: 6, padding: 10 }}>
                <div style={{ fontSize: 10, color: '#6b7280', fontWeight: 600, textTransform: 'uppercase' }}>Domain Owner</div>
                <input
                  type="text"
                  value={contract.info?.owner || ''}
                  onChange={e => updateInfo('owner', e.target.value)}
                  style={{ ...inputStyle, marginTop: 4, fontWeight: 600 }}
                  placeholder="Data Governance Owner..."
                />
              </div>

              <div style={{ background: '#f9fafb', border: '1px solid #e5e7eb', borderRadius: 6, padding: 10 }}>
                <div style={{ fontSize: 10, color: '#6b7280', fontWeight: 600, textTransform: 'uppercase' }}>Target Dataset</div>
                <input
                  type="text"
                  value={contract.info?.target_dataset || ''}
                  onChange={e => updateInfo('target_dataset', e.target.value)}
                  style={{ ...inputStyle, marginTop: 4, fontFamily: 'monospace', fontWeight: 600 }}
                  placeholder="Target BigQuery/Delta dataset..."
                />
              </div>

              <div style={{ background: '#f9fafb', border: '1px solid #e5e7eb', borderRadius: 6, padding: 10 }}>
                <div style={{ fontSize: 10, color: '#6b7280', fontWeight: 600, textTransform: 'uppercase' }}>Freshness Schedule & Max Lag</div>
                <div style={{ display: 'flex', gap: 4, marginTop: 4 }}>
                  <input
                    type="text"
                    value={contract.servicelevels?.freshness?.schedule || ''}
                    onChange={e => updateSla('freshness', 'schedule', e.target.value)}
                    style={{ ...inputStyle, flex: 1 }}
                    placeholder="DAILY_BATCH"
                  />
                  <input
                    type="text"
                    value={contract.servicelevels?.freshness?.maxLag || ''}
                    onChange={e => updateSla('freshness', 'maxLag', e.target.value)}
                    style={{ ...inputStyle, flex: 1 }}
                    placeholder="24h lag"
                  />
                </div>
              </div>

              <div style={{ background: '#f9fafb', border: '1px solid #e5e7eb', borderRadius: 6, padding: 10 }}>
                <div style={{ fontSize: 10, color: '#6b7280', fontWeight: 600, textTransform: 'uppercase' }}>Query Availability & Retention</div>
                <div style={{ display: 'flex', gap: 4, marginTop: 4 }}>
                  <input
                    type="text"
                    value={contract.servicelevels?.availability?.percentage || ''}
                    onChange={e => updateSla('availability', 'percentage', e.target.value)}
                    style={{ ...inputStyle, flex: 1 }}
                    placeholder="99.9%"
                  />
                  <input
                    type="text"
                    value={contract.servicelevels?.retention?.period || ''}
                    onChange={e => updateSla('retention', 'period', e.target.value)}
                    style={{ ...inputStyle, flex: 1 }}
                    placeholder="7 years"
                  />
                </div>
              </div>
            </div>

            {/* Governance Standards Tags */}
            <div style={{ background: '#f9fafb', border: '1px solid #e5e7eb', borderRadius: 6, padding: 12 }}>
              <div style={{ fontSize: 10.5, fontWeight: 700, color: '#374151', marginBottom: 8, textTransform: 'uppercase' }}>
                Governance & Regulatory Standards Tags:
              </div>
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
                {(contract.info?.standards || []).map((s, idx) => (
                  <span key={idx} style={{
                    background: '#E6DCFF',
                    color: '#460073',
                    border: '1px solid #C2A3FF',
                    borderRadius: 4,
                    padding: '3px 8px',
                    fontSize: 11,
                    fontWeight: 600,
                    display: 'flex',
                    alignItems: 'center',
                    gap: 6,
                  }}>
                    {s}
                    <button
                      type="button"
                      onClick={() => removeStandard(idx)}
                      title={`Remove standard ${s}`}
                      style={{
                        background: 'transparent',
                        border: 'none',
                        color: '#7500C0',
                        cursor: 'pointer',
                        fontWeight: 700,
                        padding: 0,
                        lineHeight: 1,
                      }}
                    >
                      ×
                    </button>
                  </span>
                ))}
                <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                  <input
                    type="text"
                    value={newStandardInput}
                    onChange={e => setNewStandardInput(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addStandard(); } }}
                    placeholder="+ Add standard..."
                    style={{ ...inputStyle, width: 130, padding: '3px 6px', fontSize: 10.5 }}
                  />
                  <button
                    type="button"
                    onClick={addStandard}
                    style={{
                      background: '#7500C0',
                      color: '#fff',
                      border: 'none',
                      borderRadius: 4,
                      padding: '4px 8px',
                      fontSize: 10.5,
                      fontWeight: 700,
                      cursor: 'pointer',
                    }}
                  >
                    Add
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* =================================================================== */}
        {/* TAB 4: RAW YAML SPEC (LIVE UPDATING)                                */}
        {/* =================================================================== */}
        {activeTab === 'yaml' && (
          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
              <span style={{ fontSize: 10.5, color: '#6b7280', fontWeight: 600 }}>
                OpenDataContract Standard v0.9.3 — Automatically regenerated from your live edits above
              </span>
              <div style={{ display: 'flex', gap: 6 }}>
                <button
                  type="button"
                  onClick={handleDownloadYaml}
                  style={{
                    background: '#f1f5f9',
                    color: '#334155',
                    border: '1px solid #cbd5e1',
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
                  <DownloadIcon /> Download .yaml
                </button>
                <button
                  type="button"
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
            </div>
            <pre style={{
              background: '#0f172a',
              color: '#f8fafc',
              padding: 12,
              borderRadius: 6,
              fontSize: 11,
              fontFamily: 'Consolas, Monaco, monospace',
              overflowX: 'auto',
              maxHeight: 340,
              lineHeight: 1.45,
            }}>
              {generatedYaml}
            </pre>
          </div>
        )}
      </div>

      {/* ── Bottom Action Footer ── */}
      <div style={{
        background: '#f8fafc',
        borderTop: '1px solid #e2e8f0',
        padding: '8px 14px',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        flexWrap: 'wrap',
        gap: 8,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 11, color: '#64748b' }}>
          <span>Status: <strong style={{ color: '#10B981' }}>{contract.info?.status || 'ACTIVE'}</strong></span>
          <span>·</span>
          <span>Version: <strong>v{contract.info?.version || '1.0.0'}</strong></span>
          <span>·</span>
          <span>Domain: <strong>{contract.info?.domain || 'Enterprise Gold Domain'}</strong></span>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <button
            type="button"
            onClick={handleSave}
            style={{
              background: '#10B981',
              color: '#ffffff',
              border: 'none',
              borderRadius: 6,
              padding: '6px 14px',
              fontSize: 11,
              fontWeight: 700,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: 5,
            }}
          >
            <SaveIcon /> Apply &amp; Save Contract
          </button>
        </div>
      </div>
    </div>
  )
}
