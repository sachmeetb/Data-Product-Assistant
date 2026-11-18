import { useState, useEffect } from 'react'
import { fetchDomainEntities, fetchSourceModels } from '../api/catalog'
import { addEntityToStructure, importEntityIntoStructure, saveSessionContract } from '../api/chat'

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
    standards: ['Databricks Delta Lake', 'Unity Catalog', 'BCBS 239'],
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
      type: 'dimension',
      grain: 'one row per customer party',
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
      type: 'fact',
      grain: 'one row per transaction posting event',
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
  lineage: {
    upstream_sources: [
      { system: 'Raw Core Platform / CDC Ingestion', layer: 'BRONZE', type: 'CDC_STREAM', frequency: 'HOURLY' },
      { system: 'Daily Transaction Clearing Ledger', layer: 'BRONZE', type: 'BATCH_FILE', frequency: 'DAILY' },
    ],
    entity_lineage: [
      {
        target_table: 'banking_silver.slv_customer',
        source_tables: ['raw_bronze.src_customer_master'],
        join_type: 'PRIMARY_SOURCE',
        column_lineage: [
          { target_column: 'surrogate_key', source_column: 'raw_bronze.src_customer_master.cust_id', transformation: 'SHA256_SURROGATE_HASH' },
          { target_column: 'customer_id', source_column: 'raw_bronze.src_customer_master.cust_id', transformation: 'CAST_TO_STRING' },
          { target_column: 'customer_name', source_column: 'raw_bronze.src_customer_master.cust_name', transformation: 'UPPER(TRIM())' },
          { target_column: 'kyc_status', source_column: 'raw_bronze.src_customer_master.kyc_stat_cd', transformation: 'MAP_CODE_SET' },
        ],
      },
      {
        target_table: 'banking_silver.slv_account',
        source_tables: ['raw_bronze.src_account_ledger'],
        join_type: 'PRIMARY_SOURCE',
        column_lineage: [
          { target_column: 'surrogate_key', source_column: 'raw_bronze.src_account_ledger.acct_no', transformation: 'SHA256_SURROGATE_HASH' },
          { target_column: 'account_id', source_column: 'raw_bronze.src_account_ledger.acct_no', transformation: 'CAST_TO_STRING' },
          { target_column: 'customer_id', source_column: 'raw_bronze.src_account_ledger.cust_id', transformation: 'CAST_TO_STRING' },
          { target_column: 'current_balance', source_column: 'raw_bronze.src_account_ledger.bal_amt', transformation: 'CAST_TO_NUMERIC' },
        ],
      },
      {
        target_table: 'banking_silver.slv_transaction_event',
        source_tables: ['raw_bronze.src_transaction_stream'],
        join_type: 'PRIMARY_SOURCE',
        column_lineage: [
          { target_column: 'surrogate_key', source_column: 'raw_bronze.src_transaction_stream.txn_id', transformation: 'SHA256_SURROGATE_HASH' },
          { target_column: 'transaction_id', source_column: 'raw_bronze.src_transaction_stream.txn_id', transformation: 'CAST_TO_STRING' },
          { target_column: 'account_id', source_column: 'raw_bronze.src_transaction_stream.acct_no', transformation: 'CAST_TO_STRING' },
          { target_column: 'txn_amount', source_column: 'raw_bronze.src_transaction_stream.amount', transformation: 'CAST_TO_NUMERIC' },
          { target_column: 'txn_timestamp', source_column: 'raw_bronze.src_transaction_stream.event_ts', transformation: 'PARSE_TIMESTAMP_UTC' },
        ],
      },
    ],
    downstream_consumers: [
      { consumer_role: 'Executive BI & Reporting', intended_layer: 'GOLD', use_case: 'Customer 360 KPIs & Financial Dashboards' },
      { consumer_role: 'Risk & Compliance Engine', intended_layer: 'GOLD', use_case: 'Basel III & Fraud Detection Aggregations' },
    ],
  },
}

export const DEFAULT_SILVER_CONTRACT = DEFAULT_GOLD_CONTRACT

function generateYamlString(data) {
  const lines = []
  lines.push(`dataContractSpecification: "${data.dataContractSpecification || '0.9.3'}"`)
  lines.push(`id: "${data.id || 'urn:datacontract:gold:consumption'}"`)
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
  lines.push(`    maxLag: "${data.servicelevels?.freshness?.maxLag || '4h lag'}"`)
  lines.push(`    cron: "${data.servicelevels?.freshness?.cron || '0 4 * * *'}"`)
  lines.push('  availability:')
  lines.push(`    percentage: "${data.servicelevels?.availability?.percentage || '99.95%'}"`)
  lines.push('  retention:')
  lines.push(`    period: "${data.servicelevels?.retention?.period || '7 years'}"`)
  lines.push(`    policy: "${data.servicelevels?.retention?.policy || 'GDPR Compliant Archival'}"`)

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
      if (fDef.transformSourceObjects) {
        lines.push('        transformSourceObjects:')
        fDef.transformSourceObjects.forEach(src => lines.push(`          - "${src}"`))
      }
      if (fDef.transformLogic) {
        lines.push(`        transformLogic: "${fDef.transformLogic}"`)
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

  if (data.lineage) {
    lines.push('lineage:')
    if (data.lineage.upstream_sources?.length) {
      lines.push('  upstream_sources:')
      data.lineage.upstream_sources.forEach(s => {
        lines.push(`    - system: "${s.system || ''}"`)
        lines.push(`      layer: "${s.layer || 'BRONZE'}"`)
        if (s.type) lines.push(`      type: "${s.type}"`)
        if (s.frequency) lines.push(`      frequency: "${s.frequency}"`)
      })
    }
    if (data.lineage.entity_lineage?.length) {
      lines.push('  entity_lineage:')
      data.lineage.entity_lineage.forEach(el => {
        lines.push(`    - target_table: "${el.target_table || ''}"`)
        if (el.source_tables?.length) {
          lines.push('      source_tables:')
          el.source_tables.forEach(st => lines.push(`        - "${st}"`))
        }
        if (el.column_lineage?.length) {
          lines.push('      column_lineage:')
          el.column_lineage.forEach(cl => {
            lines.push(`        - target_column: "${cl.target_column || ''}"`)
            lines.push(`          source_column: "${cl.source_column || ''}"`)
            lines.push(`          transformation: "${cl.transformation || 'DIRECT_PASS'}"`)
          })
        }
      })
    }
    if (data.lineage.downstream_consumers?.length) {
      lines.push('  downstream_consumers:')
      data.lineage.downstream_consumers.forEach(dc => {
        lines.push(`    - consumer_role: "${dc.consumer_role || ''}"`)
        lines.push(`      intended_layer: "${dc.intended_layer || 'GOLD'}"`)
        lines.push(`      use_case: "${dc.use_case || ''}"`)
      })
    }
  }

  return lines.join('\n')
}


export default function DataContractCard({ view, sessionId, onSave, onContractChange, isModal, onClose }) {
  const [activeTab, setActiveTab] = useState('schema')
  const [copied, setCopied] = useState(false)
  const [savedBadge, setSavedBadge] = useState(false)
  const [saving, setSaving] = useState(false)
  const [newStandardInput, setNewStandardInput] = useState('')

  // Structure Extension Modals / Panels: null | 'inspect_domain' | 'add_canonical' | 'create_custom' | 'import_source'
  const [extensionPanel, setExtensionPanel] = useState(null)
  const [domainEntities, setDomainEntities] = useState([])
  const [sourceModels, setSourceModels] = useState([])
  const [extensionNotice, setExtensionNotice] = useState('')
  const [extensionBusy, setExtensionBusy] = useState(false)

  // Custom Entity Form state (structured columns array)
  const [customEntityForm, setCustomEntityForm] = useState({
    table_name: 'slv_loan_collateral',
    type: 'dimension',
    scd_type: 'Type 2',
    grain: 'one row per pledged collateral asset',
    description: 'Conformed Collateral Asset & Valuation Dimension',
    columns: [
      { name: 'surrogate_key', type: 'STRING', pk: true, pii: false, description: 'Deterministic SHA-256 Surrogate Key' },
      { name: 'collateral_id', type: 'STRING', pk: false, pii: false, description: 'Collateral Asset Identifier' },
      { name: 'account_id', type: 'STRING', pk: false, pii: false, fk: 'slv_account.account_id', description: 'Secured Account Reference' },
      { name: 'collateral_type_cd', type: 'STRING', pk: false, pii: false, description: 'REAL_ESTATE, CASH_DEPOSIT, SECURITIES, EQUIPMENT' },
      { name: 'appraised_value_amt', type: 'DECIMAL(18,2)', pk: false, pii: false, description: 'Latest Appraised Valuation Amount' },
      { name: 'valuation_date', type: 'DATE', pk: false, pii: false, description: 'Appraisal Effective Date' },
    ],
  })

  // Import Entity from Source / Model state
  const [importSourceTab, setImportSourceTab] = useState('source_catalog') // 'source_catalog' | 'ddl' | 'odcs'
  const [selectedSourceModelId, setSelectedSourceModelId] = useState('raw_core.party_kyc_screening')
  const [importTargetTableName, setImportTargetTableName] = useState('')
  const [importDdlText, setImportDdlText] = useState(
`CREATE TABLE raw_core.standing_order_schedule (
  schedule_id STRING PRIMARY KEY OPTIONS(description='Standing Order Mandate ID'),
  debtor_account_id STRING NOT NULL OPTIONS(description='Originating Account FK'),
  beneficiary_iban STRING OPTIONS(description='Creditor IBAN'),
  recurring_amount NUMERIC NOT NULL OPTIONS(description='Scheduled Transfer Amount'),
  frequency_cd STRING NOT NULL OPTIONS(description='WEEKLY, MONTHLY, QUARTERLY'),
  next_execution_date DATE OPTIONS(description='Next Scheduled Run Date')
);`
  )
  const [importOdcsText, setImportOdcsText] = useState(
`models:
  slv_merchant_directory:
    description: Conformed Acquiring & Counterparty Merchant Directory
    type: dimension
    grain: one row per merchant entity
    fields:
      surrogate_key:
        type: string
        primary: true
        description: Deterministic Merchant Surrogate Key
      merchant_id:
        type: string
        nullable: false
        description: Card Network Merchant Identifier (MID)
      merchant_legal_name:
        type: string
        pii: false
        description: Registered Merchant Name
      mcc_code:
        type: string
        description: ISO 18245 4-digit Merchant Category Code
      country_code:
        type: string
        description: ISO 3166-1 Merchant Country`
  )

  // Initialize editable state with full prefilled values
  const [contract, setContract] = useState(() => {
    const base = view && typeof view === 'object' && Object.keys(view).length > 0
      ? JSON.parse(JSON.stringify(view))
      : JSON.parse(JSON.stringify(DEFAULT_GOLD_CONTRACT))

    if (!base.info) base.info = {}
    if (!base.servicelevels) base.servicelevels = { freshness: {}, availability: {}, retention: {} }
    if (!base.models || Object.keys(base.models).length === 0) base.models = DEFAULT_GOLD_CONTRACT.models
    if (!base.quality || base.quality.length === 0) base.quality = DEFAULT_GOLD_CONTRACT.quality
    if (!base.lineage) base.lineage = DEFAULT_GOLD_CONTRACT.lineage

    // Fill in default placeholders if any empty string exists
    if (!base.info.title) base.info.title = DEFAULT_GOLD_CONTRACT.info.title
    if (!base.info.owner) base.info.owner = DEFAULT_GOLD_CONTRACT.info.owner
    if (!base.info.standards || base.info.standards.length === 0) base.info.standards = DEFAULT_GOLD_CONTRACT.info.standards
    if (!base.info.target_dataset) base.info.target_dataset = DEFAULT_GOLD_CONTRACT.info.target_dataset
    if (!base.servicelevels.freshness.schedule) base.servicelevels.freshness.schedule = 'DAILY_BATCH'
    if (!base.servicelevels.freshness.maxLag) base.servicelevels.freshness.maxLag = '4h lag'

    return base
  })

  const activeDomainLabel =
    contract?.catalog_context?.domain_name ||
    contract?.info?.domain ||
    'Core Banking & Accounts'

  useEffect(() => {
    if (onContractChange) {
      const yamlString = generateYamlString(contract)
      onContractChange({ ...contract, yaml_text: yamlString })
    }
  }, [contract, onContractChange])

  // Whenever view changes externally, update contract if provided
  useEffect(() => {
    if (view && typeof view === 'object' && Object.keys(view).length > 0) {
      setContract({
        ...DEFAULT_GOLD_CONTRACT,
        ...view,
        info: { ...DEFAULT_GOLD_CONTRACT.info, ...(view.info || {}) },
        servicelevels: { ...DEFAULT_GOLD_CONTRACT.servicelevels, ...(view.servicelevels || {}) },
        models: (view.models && Object.keys(view.models).length > 0) ? view.models : DEFAULT_GOLD_CONTRACT.models,
        quality: (view.quality && view.quality.length > 0) ? view.quality : DEFAULT_GOLD_CONTRACT.quality,
        lineage: (view.lineage && typeof view.lineage === 'object') ? view.lineage : DEFAULT_GOLD_CONTRACT.lineage,
      })
      if (view.catalog_context?.all_domain_entities) {
        setDomainEntities(view.catalog_context.all_domain_entities)
      }
      if (view.catalog_context?.available_source_models) {
        setSourceModels(view.catalog_context.available_source_models)
      }
    }
  }, [view])

  // Load canonical domain entities & upstream source models if not already attached
  useEffect(() => {
    const domName = contract?.catalog_context?.domain_id || contract?.catalog_context?.domain_name || contract?.info?.domain || 'core_banking'
    fetchDomainEntities(domName)
      .then(data => {
        if (data?.entities?.length) setDomainEntities(data.entities)
        if (data?.source_models?.length) setSourceModels(data.source_models)
      })
      .catch(() => {
        fetchDomainEntities('core_banking')
          .then(data => {
            if (data?.entities?.length) setDomainEntities(data.entities)
            if (data?.source_models?.length) setSourceModels(data.source_models)
          })
          .catch(() => {})
      })
    fetchSourceModels()
      .then(data => {
        if (data?.source_models?.length) setSourceModels(data.source_models)
      })
      .catch(() => {})
  }, [contract?.info?.domain, contract?.catalog_context?.domain_id])

  const flashStatus = (msg) => {
    setExtensionNotice(msg)
    setTimeout(() => setExtensionNotice(''), 4000)
  }

  const mergeEntityIntoLocalContract = (entObj) => {
    const tname = entObj.table_name || entObj.name || 'slv_entity'
    const targetDs = contract.info?.target_dataset || 'gold'
    const fieldsMap = {}
    const colLineage = []
    const srcTable = entObj.source_table || `raw_bronze.${tname.replace('slv_', 'src_')}`

    ;(entObj.columns || []).forEach(c => {
      const cname = c.name || c.column_name || 'id'
      const dtype = String(c.data_type || c.type || 'string').toLowerCase()
      const isPk = !!(c.is_pk || c.is_primary_key || c.primary_key || c.pk)
      const fkRef = c.fk_ref || c.references || c.fk || ''
      const isPii = !!(c.pii || c.is_pii)
      fieldsMap[cname] = {
        type: dtype,
        description: c.description || `Conformed attribute ${cname}`,
        primary: isPk,
        nullable: !isPk && c.nullable !== false,
        pii: isPii,
        ...(fkRef ? { references: fkRef } : {}),
      }
      colLineage.push({
        target_column: cname,
        source_column: `${srcTable}.${cname}`,
        transformation: isPk ? 'SHA256_SURROGATE_HASH(natural_key, source_system)' : 'TRIM_CLEANSE(source_val)',
      })
    })

    setContract(prev => {
      const existingLineage = (prev.lineage?.entity_lineage || []).filter(
        el => el.target_table !== `${targetDs}.${tname}` && el.target_table !== tname
      )
      return {
        ...prev,
        models: {
          ...(prev.models || {}),
          [tname]: {
            description: entObj.description || `Conformed Gold entity ${tname}`,
            type: 'table',
            entity_type: entObj.type || entObj.entity_type || 'dimension',
            grain: entObj.grain || 'one row per conformed record',
            physicalName: `${targetDs}.${tname}`,
            fields: fieldsMap,
          },
        },
        lineage: {
          ...(prev.lineage || {}),
          entity_lineage: [
            ...existingLineage,
            {
              target_table: `${targetDs}.${tname}`,
              source_tables: [srcTable],
              join_type: 'PRIMARY_SOURCE',
              column_lineage: colLineage,
            },
          ],
        },
      }
    })
  }

  const removeEntityFromContract = (modelName) => {
    const targetDs = contract.info?.target_dataset || 'gold'
    setContract(prev => {
      const nextModels = { ...(prev.models || {}) }
      delete nextModels[modelName]
      const nextLineage = (prev.lineage?.entity_lineage || []).filter(
        el => el.target_table !== modelName && el.target_table !== `${targetDs}.${modelName}`
      )
      return {
        ...prev,
        models: nextModels,
        lineage: {
          ...(prev.lineage || {}),
          entity_lineage: nextLineage,
        },
      }
    })
    flashStatus(`Removed entity '${modelName}' from structure`)
  }

  const handleAddCanonicalEntity = async (entObj) => {
    setExtensionBusy(true)
    mergeEntityIntoLocalContract(entObj)
    flashStatus(`Added canonical entity '${entObj.table_name}' to Gold structure`)
    try {
      const res = await addEntityToStructure(sessionId, {
        entity_name: entObj.table_name,
        from_catalog: true,
        domain_name: activeDomainLabel,
      })
      if (res?.contract) setContract(res.contract)
    } catch {
      // Local state already updated
    } finally {
      setExtensionBusy(false)
    }
  }

  const handleCreateCustomEntitySubmit = async () => {
    setExtensionBusy(true)
    let tname = (customEntityForm.table_name || 'slv_custom_entity').trim()
    if (!tname.startsWith('slv_')) tname = `slv_${tname}`
    const cols = (customEntityForm.columns || []).map(c => ({
      name: (c.name || 'id').trim(),
      column_name: (c.name || 'id').trim(),
      data_type: (c.type || 'STRING').toUpperCase(),
      type: (c.type || 'STRING').toUpperCase(),
      is_pk: Boolean(c.pk),
      pii: Boolean(c.pii),
      fk_ref: c.fk || undefined,
      nullable: !c.pk,
      description: c.description || `Custom attribute ${c.name}`,
    }))

    const entObj = {
      table_name: tname,
      type: customEntityForm.type || 'dimension',
      scd_type: customEntityForm.scd_type || 'Type 2',
      grain: customEntityForm.grain || 'one row per conformed record',
      description: customEntityForm.description || `Custom Gold entity ${tname}`,
      columns: cols,
    }
    mergeEntityIntoLocalContract(entObj)
    setExtensionPanel(null)
    flashStatus(`Created custom Gold entity '${tname}' (${cols.length} columns)`)

    try {
      const res = await addEntityToStructure(sessionId, {
        entity_name: tname,
        from_catalog: false,
        entity_type: entObj.type,
        scd_type: entObj.scd_type,
        grain: entObj.grain,
        description: entObj.description,
        columns: cols,
      })
      if (res?.contract) setContract(res.contract)
    } catch {
      // Local state already updated
    } finally {
      setExtensionBusy(false)
    }
  }

  const handleImportExternalEntitySubmit = async () => {
    setExtensionBusy(true)
    try {
      const res = await importEntityIntoStructure(sessionId, {
        source_type: importSourceTab,
        source_model_id: importSourceTab === 'source_catalog' ? selectedSourceModelId : undefined,
        payload: importSourceTab === 'ddl' ? importDdlText : importSourceTab === 'odcs' ? importOdcsText : '',
        entity_name: importTargetTableName.trim() || undefined,
      })
      if (res?.imported_entity) {
        mergeEntityIntoLocalContract(res.imported_entity)
        flashStatus(`Brought '${res.imported_entity.table_name}' into Gold model from ${importSourceTab.toUpperCase()}`)
      }
      if (res?.contract) {
        setContract(res.contract)
      }
      setExtensionPanel(null)
    } catch {
      // Fallback client-side import if backend unreachable
      if (importSourceTab === 'source_catalog') {
        const found = sourceModels.find(
          sm => sm.model_id === selectedSourceModelId || sm.source_id === selectedSourceModelId || sm.target_table_name === selectedSourceModelId
        )
        if (found) {
          mergeEntityIntoLocalContract({
            table_name: importTargetTableName.trim() || found.target_table_name,
            type: found.entity_type,
            grain: found.grain,
            description: found.description,
            columns: found.columns,
            source_table: found.source_id,
          })
          flashStatus(`Brought '${found.target_table_name}' into Gold model`)
        }
      }
      setExtensionPanel(null)
    } finally {
      setExtensionBusy(false)
    }
  }


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

  // Lineage mutators
  const updateUpstreamSource = (idx, prop, val) => {
    setContract(prev => {
      const sources = [...(prev.lineage?.upstream_sources || [])]
      sources[idx] = { ...sources[idx], [prop]: val }
      return {
        ...prev,
        lineage: { ...(prev.lineage || {}), upstream_sources: sources },
      }
    })
  }

  const addUpstreamSource = () => {
    setContract(prev => ({
      ...prev,
      lineage: {
        ...(prev.lineage || {}),
        upstream_sources: [
          ...(prev.lineage?.upstream_sources || []),
          { system: 'Raw Source Feed / File', layer: 'BRONZE', type: 'CDC_STREAM', frequency: 'HOURLY' },
        ],
      },
    }))
  }

  const deleteUpstreamSource = (idx) => {
    setContract(prev => ({
      ...prev,
      lineage: {
        ...(prev.lineage || {}),
        upstream_sources: (prev.lineage?.upstream_sources || []).filter((_, i) => i !== idx),
      },
    }))
  }

  const updateColumnLineage = (tableIdx, colIdx, prop, val) => {
    setContract(prev => {
      const entityLineage = [...(prev.lineage?.entity_lineage || [])]
      if (!entityLineage[tableIdx]) return prev
      const colLineage = [...(entityLineage[tableIdx].column_lineage || [])]
      colLineage[colIdx] = { ...colLineage[colIdx], [prop]: val }
      entityLineage[tableIdx] = { ...entityLineage[tableIdx], column_lineage: colLineage }
      return {
        ...prev,
        lineage: { ...(prev.lineage || {}), entity_lineage: entityLineage },
      }
    })
  }

  const addColumnLineage = (tableIdx) => {
    setContract(prev => {
      const entityLineage = [...(prev.lineage?.entity_lineage || [])]
      if (!entityLineage[tableIdx]) return prev
      const colLineage = [
        ...(entityLineage[tableIdx].column_lineage || []),
        { target_column: 'new_attribute', source_column: 'raw_bronze.column', transformation: 'DIRECT_PASS' },
      ]
      entityLineage[tableIdx] = { ...entityLineage[tableIdx], column_lineage: colLineage }
      return {
        ...prev,
        lineage: { ...(prev.lineage || {}), entity_lineage: entityLineage },
      }
    })
  }

  const deleteColumnLineage = (tableIdx, colIdx) => {
    setContract(prev => {
      const entityLineage = [...(prev.lineage?.entity_lineage || [])]
      if (!entityLineage[tableIdx]) return prev
      const colLineage = (entityLineage[tableIdx].column_lineage || []).filter((_, i) => i !== colIdx)
      entityLineage[tableIdx] = { ...entityLineage[tableIdx], column_lineage: colLineage }
      return {
        ...prev,
        lineage: { ...(prev.lineage || {}), entity_lineage: entityLineage },
      }
    })
  }

  const updateEntityLineage = (idx, prop, val) => {
    setContract(prev => {
      const entityLineage = [...(prev.lineage?.entity_lineage || [])]
      if (!entityLineage[idx]) return prev
      entityLineage[idx] = { ...entityLineage[idx], [prop]: val }
      return {
        ...prev,
        lineage: { ...(prev.lineage || {}), entity_lineage: entityLineage },
      }
    })
  }

  const addEntityLineage = () => {
    const firstModel = Object.keys(contract.models || {})[0] || 'silver_entity'
    setContract(prev => ({
      ...prev,
      lineage: {
        ...(prev.lineage || {}),
        entity_lineage: [
          ...(prev.lineage?.entity_lineage || []),
          {
            target_table: firstModel,
            source_table: `raw_bronze_${firstModel}`,
            join_key: 'id',
            column_lineage: [
              { target_column: 'id', source_column: 'id', transformation: 'DIRECT_PASS' },
              { target_column: 'updated_at', source_column: 'ingest_ts', transformation: 'CURRENT_TIMESTAMP()' },
            ],
          },
        ],
      },
    }))
  }

  const deleteEntityLineage = (idx) => {
    setContract(prev => ({
      ...prev,
      lineage: {
        ...(prev.lineage || {}),
        entity_lineage: (prev.lineage?.entity_lineage || []).filter((_, i) => i !== idx),
      },
    }))
  }

  // Save Contract

  const handleSave = async () => {
    if (saving) return
    setSaving(true)
    const updatedContract = {
      ...contract,
      info: {
        ...(contract.info || {}),
        status: 'APPROVED',
      },
    }
    setContract(updatedContract)
    const yamlString = generateYamlString(updatedContract)
    const finalized = { ...updatedContract, yaml_text: yamlString }
    try {
      if (sessionId) {
        await saveSessionContract(sessionId, finalized).catch(() => {})
      }
      if (onSave) {
        await onSave(finalized)
      }
    } finally {
      setSaving(false)
      setSavedBadge(true)
      setTimeout(() => setSavedBadge(false), 3500)
      if (isModal && onClose && onSave) {
        onClose()
      }
    }
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
    a.download = `${contract.info?.title?.toLowerCase().replace(/\s+/g, '_') || 'silver_contract'}.yaml`
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
              {contract.id || 'urn:datacontract:banking_silver'}
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
              <CheckIcon /> Contract Applied &amp; Saved!
            </span>
          )}

          <button
            type="button"
            onClick={handleSave}
            disabled={saving}
            title="Apply and save your changes to this Data Contract and generate Gold DDL & STTM artifacts"
            style={{
              background: saving ? '#059669' : '#10B981',
              color: '#ffffff',
              border: 'none',
              borderRadius: 6,
              padding: '6px 12px',
              fontSize: 11,
              fontWeight: 700,
              cursor: saving ? 'wait' : 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: 5,
              boxShadow: '0 1px 3px rgba(0,0,0,0.2)',
              transition: 'background 0.15s',
              opacity: saving ? 0.85 : 1,
            }}
          >
            <SaveIcon /> {saving ? 'Saving & Generating...' : 'Save Contract'}
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
          💡 <strong>Values are pre-filled below:</strong> Click into any text or attribute to edit or delete the previous value, or extend the Gold structure using the toolbar below.
        </span>
        <span style={{ fontSize: 10.5, color: '#7E22CE', fontWeight: 600 }}>
          {Object.keys(contract.models || {}).length} Models · {(contract.lineage?.entity_lineage || []).length} Lineage Mappings · {contract.quality?.length || 0} Quality Rules
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
          { id: 'lineage', label: `Lineage & Provenance` },
          { id: 'quality', label: `Quality Rules (${contract.quality?.length || 0})` },
          { id: 'overview', label: 'Overview & SLAs' },
          { id: 'yaml', label: 'Live YAML Spec' },
        ].map(tab => (

          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            style={{
              background: activeTab === tab.id ? '#ffffff' : 'transparent',
              color: activeTab === tab.id ? '#460073' : '#460073',
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
        {/* TAB 1: MODELS & COLUMNS (EDITABLE + STRUCTURE EXTENSION)            */}
        {/* =================================================================== */}
        {activeTab === 'schema' && (
          <div>
            {/* ── Gold structure Extension Action Bar ── */}
            <div style={{
              background: '#f8fafc',
              border: '1px solid #e2e8f0',
              borderRadius: 8,
              padding: '10px 12px',
              marginBottom: 12,
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              flexWrap: 'wrap',
              gap: 8,
            }}>
              <div>
                <div style={{ fontSize: 12, fontWeight: 700, color: '#1e1b4b' }}>
                  Gold structure Workbench — {activeDomainLabel}
                </div>
                <div style={{ fontSize: 11, color: '#64748b' }}>
                  Inspect associated domain entities, edit active tables, add canonical/custom entities, or import from an external source/model.
                </div>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                <button
                  type="button"
                  onClick={() => setExtensionPanel(extensionPanel === 'inspect_domain' ? null : 'inspect_domain')}
                  style={{
                    background: extensionPanel === 'inspect_domain' ? '#460073' : '#ffffff',
                    color: extensionPanel === 'inspect_domain' ? '#ffffff' : '#460073',
                    border: '1px solid #C2A3FF',
                    borderRadius: 6,
                    padding: '5px 10px',
                    fontSize: 11,
                    fontWeight: 700,
                    cursor: 'pointer',
                  }}
                >
                  🔍 Inspect Area Entities ({domainEntities.length})
                </button>
                <button
                  type="button"
                  onClick={() => setExtensionPanel(extensionPanel === 'add_canonical' ? null : 'add_canonical')}
                  style={{
                    background: extensionPanel === 'add_canonical' ? '#7500C0' : '#F3E8FF',
                    color: extensionPanel === 'add_canonical' ? '#ffffff' : '#460073',
                    border: '1px solid #C2A3FF',
                    borderRadius: 6,
                    padding: '5px 10px',
                    fontSize: 11,
                    fontWeight: 700,
                    cursor: 'pointer',
                  }}
                >
                  ➕ Add Canonical Entity
                </button>
                <button
                  type="button"
                  onClick={() => setExtensionPanel(extensionPanel === 'create_custom' ? null : 'create_custom')}
                  style={{
                    background: extensionPanel === 'create_custom' ? '#7500C0' : '#EEF2FF',
                    color: extensionPanel === 'create_custom' ? '#ffffff' : '#3730a3',
                    border: '1px solid #c7d2fe',
                    borderRadius: 6,
                    padding: '5px 10px',
                    fontSize: 11,
                    fontWeight: 700,
                    cursor: 'pointer',
                  }}
                >
                  ✨ Create Custom Entity
                </button>
                <button
                  type="button"
                  onClick={() => setExtensionPanel(extensionPanel === 'import_source' ? null : 'import_source')}
                  style={{
                    background: extensionPanel === 'import_source' ? '#047857' : '#ECFDF5',
                    color: extensionPanel === 'import_source' ? '#ffffff' : '#065f46',
                    border: '1px solid #a7f3d0',
                    borderRadius: 6,
                    padding: '5px 10px',
                    fontSize: 11,
                    fontWeight: 700,
                    cursor: 'pointer',
                  }}
                >
                  📥 Bring Entity from Source/Model
                </button>
              </div>
            </div>

            {extensionNotice && (
              <div style={{
                background: '#ECFDF5',
                border: '1px solid #6EE7B7',
                color: '#065F46',
                borderRadius: 6,
                padding: '7px 12px',
                marginBottom: 12,
                fontSize: 11.5,
                fontWeight: 600,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
              }}>
                <span>✓ {extensionNotice}</span>
                <button
                  type="button"
                  onClick={() => setExtensionNotice('')}
                  style={{ background: 'none', border: 'none', color: '#065F46', cursor: 'pointer', fontWeight: 700 }}
                >
                  ✕
                </button>
              </div>
            )}

            {/* ── PANEL A: Inspect Domain Entities ── */}
            {extensionPanel === 'inspect_domain' && (
              <div style={{
                background: '#FAF5FF',
                border: '1px solid #D8B4FE',
                borderRadius: 8,
                padding: 12,
                marginBottom: 14,
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                  <div>
                    <strong style={{ fontSize: 12, color: '#460073' }}>
                      Entities Associated with {activeDomainLabel} ({domainEntities.length} canonical tables)
                    </strong>
                    <div style={{ fontSize: 11, color: '#6b21a8' }}>
                      Inspect which entities are currently active in your Gold structure vs. available in the domain catalog.
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => setExtensionPanel(null)}
                    style={{ background: 'none', border: 'none', color: '#6b21a8', cursor: 'pointer', fontWeight: 700 }}
                  >
                    ✕ Close
                  </button>
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: 8 }}>
                  {domainEntities.map(ent => {
                    const isIncluded = Boolean(contract.models?.[ent.table_name])
                    return (
                      <div
                        key={ent.table_name}
                        style={{
                          background: '#ffffff',
                          border: isIncluded ? '1.5px solid #10B981' : '1px solid #e2e8f0',
                          borderRadius: 7,
                          padding: '8px 10px',
                          display: 'flex',
                          flexDirection: 'column',
                          justifyContent: 'space-between',
                          gap: 6,
                        }}
                      >
                        <div>
                          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 6 }}>
                            <span style={{ fontFamily: 'monospace', fontWeight: 700, fontSize: 11.5, color: '#1e1b4b' }}>
                              {ent.table_name}
                            </span>
                            <span style={{
                              fontSize: 9.5,
                              fontWeight: 700,
                              padding: '1px 6px',
                              borderRadius: 10,
                              background: isIncluded ? '#D1FAE5' : '#F1F5F9',
                              color: isIncluded ? '#065F46' : '#64748B',
                            }}>
                              {isIncluded ? '✓ In Structure' : 'Available'}
                            </span>
                          </div>
                          <div style={{ fontSize: 10.5, color: '#475569', marginTop: 3 }}>
                            {ent.description}
                          </div>
                        </div>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', borderTop: '1px solid #f1f5f9', paddingTop: 5 }}>
                          <span style={{ fontSize: 10, color: '#64748b' }}>
                            {(ent.columns || []).length} columns · SCD {ent.scd_type || 'Type 2'}
                          </span>
                          {!isIncluded ? (
                            <button
                              type="button"
                              disabled={extensionBusy}
                              onClick={() => handleAddCanonicalEntity(ent)}
                              style={{
                                background: '#7500C0',
                                color: '#ffffff',
                                border: 'none',
                                borderRadius: 5,
                                padding: '3px 8px',
                                fontSize: 10,
                                fontWeight: 700,
                                cursor: 'pointer',
                              }}
                            >
                              + Add to Structure
                            </button>
                          ) : (
                            <button
                              type="button"
                              onClick={() => removeEntityFromContract(ent.table_name)}
                              style={{
                                background: '#FEE2E2',
                                color: '#991B1B',
                                border: 'none',
                                borderRadius: 5,
                                padding: '3px 8px',
                                fontSize: 10,
                                fontWeight: 600,
                                cursor: 'pointer',
                              }}
                            >
                              Remove
                            </button>
                          )}
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>
            )}

            {/* ── PANEL B: Add Another Canonical Entity ── */}
            {extensionPanel === 'add_canonical' && (
              <div style={{
                background: '#F8FAFC',
                border: '1px solid #CBD5E1',
                borderRadius: 8,
                padding: 12,
                marginBottom: 14,
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                  <strong style={{ fontSize: 12, color: '#1e293b' }}>
                    Add Another Canonical Entity from {activeDomainLabel}
                  </strong>
                  <button
                    type="button"
                    onClick={() => setExtensionPanel(null)}
                    style={{ background: 'none', border: 'none', color: '#64748b', cursor: 'pointer', fontWeight: 700 }}
                  >
                    ✕ Close
                  </button>
                </div>
                {domainEntities.filter(ent => !contract.models?.[ent.table_name]).length === 0 ? (
                  <div style={{ fontSize: 11.5, color: '#065F46', background: '#ECFDF5', padding: '8px 10px', borderRadius: 6 }}>
                    All canonical entities for <strong>{activeDomainLabel}</strong> are already included in this Gold structure! You can still create a custom entity or import one from an external source/model.
                  </div>
                ) : (
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                    {domainEntities
                      .filter(ent => !contract.models?.[ent.table_name])
                      .map(ent => (
                        <div
                          key={ent.table_name}
                          style={{
                            background: '#ffffff',
                            border: '1px solid #cbd5e1',
                            borderRadius: 7,
                            padding: '8px 12px',
                            display: 'flex',
                            alignItems: 'center',
                            gap: 10,
                          }}
                        >
                          <div>
                            <div style={{ fontFamily: 'monospace', fontWeight: 700, fontSize: 11.5, color: '#460073' }}>
                              {ent.table_name}
                            </div>
                            <div style={{ fontSize: 10.5, color: '#64748b' }}>
                              {ent.description} ({(ent.columns || []).length} cols)
                            </div>
                          </div>
                          <button
                            type="button"
                            disabled={extensionBusy}
                            onClick={() => handleAddCanonicalEntity(ent)}
                            style={{
                              background: '#7500C0',
                              color: '#ffffff',
                              border: 'none',
                              borderRadius: 5,
                              padding: '5px 10px',
                              fontSize: 10.5,
                              fontWeight: 700,
                              cursor: 'pointer',
                              whiteSpace: 'nowrap',
                            }}
                          >
                            + Add Entity
                          </button>
                        </div>
                      ))}
                  </div>
                )}
              </div>
            )}

            {/* ── PANEL C: Create Custom Entity ── */}
            {extensionPanel === 'create_custom' && (
              <div style={{
                background: '#EEF2FF',
                border: '1px solid #A5B4FC',
                borderRadius: 8,
                padding: 12,
                marginBottom: 14,
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
                  <div>
                    <strong style={{ fontSize: 12, color: '#312E81' }}>
                      ✨ Create a Custom Gold entity
                    </strong>
                    <div style={{ fontSize: 11, color: '#4338CA' }}>
                      Define a custom conformed table and its columns to extend this Silver data structure.
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => setExtensionPanel(null)}
                    style={{ background: 'none', border: 'none', color: '#4338CA', cursor: 'pointer', fontWeight: 700 }}
                  >
                    ✕ Close
                  </button>
                </div>

                <div style={{ display: 'grid', gridTemplateColumns: '1fr 2fr 120px', gap: 8, marginBottom: 10 }}>
                  <div>
                    <label style={{ fontSize: 10.5, fontWeight: 700, color: '#312E81', display: 'block', marginBottom: 3 }}>
                      Entity Table Name *
                    </label>
                    <input
                      type="text"
                      value={customEntityForm.table_name}
                      onChange={e => setCustomEntityForm(prev => ({ ...prev, table_name: e.target.value }))}
                      placeholder="slv_account_overdraft_limit"
                      style={{ ...inputStyle, fontFamily: 'monospace' }}
                    />
                  </div>
                  <div>
                    <label style={{ fontSize: 10.5, fontWeight: 700, color: '#312E81', display: 'block', marginBottom: 3 }}>
                      Business Description
                    </label>
                    <input
                      type="text"
                      value={customEntityForm.description}
                      onChange={e => setCustomEntityForm(prev => ({ ...prev, description: e.target.value }))}
                      placeholder="Conformed overdraft facility limits and utilization per account"
                      style={inputStyle}
                    />
                  </div>
                  <div>
                    <label style={{ fontSize: 10.5, fontWeight: 700, color: '#312E81', display: 'block', marginBottom: 3 }}>
                      SCD Strategy
                    </label>
                    <select
                      value={customEntityForm.scd_type}
                      onChange={e => setCustomEntityForm(prev => ({ ...prev, scd_type: e.target.value }))}
                      style={inputStyle}
                    >
                      <option value="Type 2">SCD Type 2</option>
                      <option value="Type 1">SCD Type 1</option>
                      <option value="Append-Only">Append-Only</option>
                    </select>
                  </div>
                </div>

                <div style={{ marginBottom: 8 }}>
                  <div style={{ fontSize: 10.5, fontWeight: 700, color: '#312E81', marginBottom: 4 }}>
                    Columns ({customEntityForm.columns.length})
                  </div>
                  {customEntityForm.columns.map((col, idx) => (
                    <div key={idx} style={{ display: 'grid', gridTemplateColumns: '1.2fr 1fr 2fr auto auto', gap: 6, marginBottom: 5, alignItems: 'center' }}>
                      <input
                        type="text"
                        value={col.name}
                        onChange={e => {
                          const next = [...customEntityForm.columns]
                          next[idx] = { ...next[idx], name: e.target.value }
                          setCustomEntityForm(prev => ({ ...prev, columns: next }))
                        }}
                        placeholder="column_name"
                        style={{ ...inputStyle, fontFamily: 'monospace' }}
                      />
                      <select
                        value={col.type}
                        onChange={e => {
                          const next = [...customEntityForm.columns]
                          next[idx] = { ...next[idx], type: e.target.value }
                          setCustomEntityForm(prev => ({ ...prev, columns: next }))
                        }}
                        style={{ ...inputStyle, fontFamily: 'monospace' }}
                      >
                        <option value="STRING">STRING</option>
                        <option value="DECIMAL(18,2)">DECIMAL(18,2)</option>
                        <option value="INT">INT</option>
                        <option value="BIGINT">BIGINT</option>
                        <option value="BOOLEAN">BOOLEAN</option>
                        <option value="DATE">DATE</option>
                        <option value="TIMESTAMP">TIMESTAMP</option>
                      </select>
                      <input
                        type="text"
                        value={col.description}
                        onChange={e => {
                          const next = [...customEntityForm.columns]
                          next[idx] = { ...next[idx], description: e.target.value }
                          setCustomEntityForm(prev => ({ ...prev, columns: next }))
                        }}
                        placeholder="Column business description..."
                        style={inputStyle}
                      />
                      <label style={{ fontSize: 10.5, display: 'flex', alignItems: 'center', gap: 3, color: '#312E81', whiteSpace: 'nowrap' }}>
                        <input
                          type="checkbox"
                          checked={Boolean(col.pk)}
                          onChange={e => {
                            const next = [...customEntityForm.columns]
                            next[idx] = { ...next[idx], pk: e.target.checked }
                            setCustomEntityForm(prev => ({ ...prev, columns: next }))
                          }}
                        />
                        PK
                      </label>
                      <button
                        type="button"
                        onClick={() => {
                          const next = customEntityForm.columns.filter((_, i) => i !== idx)
                          setCustomEntityForm(prev => ({ ...prev, columns: next }))
                        }}
                        style={{ background: '#FEE2E2', color: '#991B1B', border: 'none', borderRadius: 4, padding: '4px 7px', cursor: 'pointer', fontSize: 10.5 }}
                      >
                        ✕
                      </button>
                    </div>
                  ))}
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 8 }}>
                    <button
                      type="button"
                      onClick={() => setCustomEntityForm(prev => ({
                        ...prev,
                        columns: [...prev.columns, { name: `custom_col_${prev.columns.length + 1}`, type: 'STRING', pk: false, pii: false, description: '' }],
                      }))}
                      style={{
                        background: '#ffffff',
                        color: '#3730a3',
                        border: '1px solid #a5b4fc',
                        borderRadius: 5,
                        padding: '4px 10px',
                        fontSize: 11,
                        fontWeight: 600,
                        cursor: 'pointer',
                      }}
                    >
                      + Add Column Row
                    </button>
                    <button
                      type="button"
                      disabled={extensionBusy || !customEntityForm.table_name.trim()}
                      onClick={handleCreateCustomEntitySubmit}
                      style={{
                        background: '#4F46E5',
                        color: '#ffffff',
                        border: 'none',
                        borderRadius: 6,
                        padding: '6px 14px',
                        fontSize: 11,
                        fontWeight: 700,
                        cursor: 'pointer',
                      }}
                    >
                      ✓ Add Custom Entity to Gold structure
                    </button>
                  </div>
                </div>
              </div>
            )}

            {/* ── PANEL D: Bring Entity from Existing Source / Model ── */}
            {extensionPanel === 'import_source' && (
              <div style={{
                background: '#ECFDF5',
                border: '1px solid #6EE7B7',
                borderRadius: 8,
                padding: 12,
                marginBottom: 14,
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
                  <div>
                    <strong style={{ fontSize: 12, color: '#065F46' }}>
                      📥 Bring an Entity into the Model from an Existing Source / Model
                    </strong>
                    <div style={{ fontSize: 11, color: '#047857' }}>
                      Import a table from upstream Bronze / cross-domain catalog, paste a SQL CREATE TABLE DDL snippet, or paste an ODCS YAML/JSON model.
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => setExtensionPanel(null)}
                    style={{ background: 'none', border: 'none', color: '#065F46', cursor: 'pointer', fontWeight: 700 }}
                  >
                    ✕ Close
                  </button>
                </div>

                {/* Source Type Tabs */}
                <div style={{ display: 'flex', gap: 6, marginBottom: 10 }}>
                  {[
                    { id: 'source_catalog', label: '1. Existing Bronze / Domain Source Catalog' },
                    { id: 'ddl', label: '2. SQL CREATE TABLE DDL' },
                    { id: 'odcs', label: '3. ODCS YAML / JSON Snippet' },
                  ].map(st => (
                    <button
                      key={st.id}
                      type="button"
                      onClick={() => setImportSourceTab(st.id)}
                      style={{
                        background: importSourceTab === st.id ? '#047857' : '#ffffff',
                        color: importSourceTab === st.id ? '#ffffff' : '#065F46',
                        border: '1px solid #6EE7B7',
                        borderRadius: 6,
                        padding: '5px 10px',
                        fontSize: 11,
                        fontWeight: 700,
                        cursor: 'pointer',
                      }}
                    >
                      {st.label}
                    </button>
                  ))}
                </div>

                {importSourceTab === 'source_catalog' && (
                  <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr auto', gap: 8, alignItems: 'end' }}>
                    <div>
                      <label style={{ fontSize: 10.5, fontWeight: 700, color: '#065F46', display: 'block', marginBottom: 3 }}>
                        Select Upstream Bronze or Cross-Domain Model ({sourceModels.length} available)
                      </label>
                      <select
                        value={selectedSourceModelId}
                        onChange={e => setSelectedSourceModelId(e.target.value)}
                        style={inputStyle}
                      >
                        {sourceModels.map(sm => (
                          <option key={sm.model_id} value={sm.model_id}>
                            [{sm.source_system}] {sm.source_table} → {sm.target_silver_table} ({sm.domain_label} · {(sm.columns || []).length} cols)
                          </option>
                        ))}
                      </select>
                    </div>
                    <div>
                      <label style={{ fontSize: 10.5, fontWeight: 700, color: '#065F46', display: 'block', marginBottom: 3 }}>
                        Target Gold Table Name (Optional Override)
                      </label>
                      <input
                        type="text"
                        value={importTargetTableName}
                        onChange={e => setImportTargetTableName(e.target.value)}
                        placeholder="Auto-conformed slv_* name"
                        style={{ ...inputStyle, fontFamily: 'monospace' }}
                      />
                    </div>
                    <button
                      type="button"
                      disabled={extensionBusy}
                      onClick={handleImportExternalEntitySubmit}
                      style={{
                        background: '#047857',
                        color: '#ffffff',
                        border: 'none',
                        borderRadius: 6,
                        padding: '6px 14px',
                        fontSize: 11,
                        fontWeight: 700,
                        cursor: 'pointer',
                      }}
                    >
                      📥 Import into Gold model
                    </button>
                  </div>
                )}

                {importSourceTab === 'ddl' && (
                  <div>
                    <label style={{ fontSize: 10.5, fontWeight: 700, color: '#065F46', display: 'block', marginBottom: 4 }}>
                      Paste SQL DDL (`CREATE TABLE ...`) to reverse-engineer and conform into Gold:
                    </label>
                    <textarea
                      rows={5}
                      value={importDdlText}
                      onChange={e => setImportDdlText(e.target.value)}
                      style={{ ...inputStyle, fontFamily: 'monospace', fontSize: 11, marginBottom: 8 }}
                    />
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <input
                        type="text"
                        value={importTargetTableName}
                        onChange={e => setImportTargetTableName(e.target.value)}
                        placeholder="Optional override target table name (e.g. slv_standing_order)"
                        style={{ ...inputStyle, width: 320, fontFamily: 'monospace' }}
                      />
                      <button
                        type="button"
                        disabled={extensionBusy || !importDdlText.trim()}
                        onClick={handleImportExternalEntitySubmit}
                        style={{
                          background: '#047857',
                          color: '#ffffff',
                          border: 'none',
                          borderRadius: 6,
                          padding: '6px 14px',
                          fontSize: 11,
                          fontWeight: 700,
                          cursor: 'pointer',
                        }}
                      >
                        📥 Parse DDL & Bring into Gold model
                      </button>
                    </div>
                  </div>
                )}

                {importSourceTab === 'odcs' && (
                  <div>
                    <label style={{ fontSize: 10.5, fontWeight: 700, color: '#065F46', display: 'block', marginBottom: 4 }}>
                      Paste Open Data Contract Standard (ODCS) YAML or JSON model definition:
                    </label>
                    <textarea
                      rows={6}
                      value={importOdcsText}
                      onChange={e => setImportOdcsText(e.target.value)}
                      style={{ ...inputStyle, fontFamily: 'monospace', fontSize: 11, marginBottom: 8 }}
                    />
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <input
                        type="text"
                        value={importTargetTableName}
                        onChange={e => setImportTargetTableName(e.target.value)}
                        placeholder="Optional override target table name"
                        style={{ ...inputStyle, width: 320, fontFamily: 'monospace' }}
                      />
                      <button
                        type="button"
                        disabled={extensionBusy || !importOdcsText.trim()}
                        onClick={handleImportExternalEntitySubmit}
                        style={{
                          background: '#047857',
                          color: '#ffffff',
                          border: 'none',
                          borderRadius: 6,
                          padding: '6px 14px',
                          fontSize: 11,
                          fontWeight: 700,
                          cursor: 'pointer',
                        }}
                      >
                        📥 Parse ODCS & Bring into Gold model
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )}

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
                    {Object.keys(contract.models || {}).length > 1 && (
                      <button
                        type="button"
                        onClick={() => removeEntityFromContract(modelName)}
                        style={{
                          background: '#FEE2E2',
                          color: '#991B1B',
                          border: '1px solid #FECACA',
                          borderRadius: 5,
                          padding: '4px 8px',
                          fontSize: 10.5,
                          fontWeight: 600,
                          cursor: 'pointer',
                          whiteSpace: 'nowrap',
                        }}
                        title="Remove this entity from the Gold structure"
                      >
                        ✕ Remove Entity
                      </button>
                    )}
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
                                  title="Foreign key reference (e.g. slv_customer.customer_id)"
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
        {/* TAB: LINEAGE & PROVENANCE (ODCS / EDITABLE)                         */}
        {/* =================================================================== */}
        {activeTab === 'lineage' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
            {/* Upstream Sources Header & Cards */}
            <div style={{ background: '#f8fafc', border: '1px solid #e2e8f0', borderRadius: 8, padding: 14 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
                <div>
                  <h4 style={{ margin: 0, fontSize: 13, fontWeight: 700, color: '#1e293b' }}>
                    1. Upstream Data Sources (Bronze / Ingestion Layer)
                  </h4>
                  <div style={{ fontSize: 11, color: '#64748b', marginTop: 2 }}>
                    Systems, file feeds, and CDC streams feeding into this Conformed Gold Data Product
                  </div>
                </div>
                <button
                  type="button"
                  onClick={addUpstreamSource}
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
                  <PlusIcon /> Add Upstream Source
                </button>
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 10 }}>
                {(contract.lineage?.upstream_sources || []).map((src, idx) => (
                  <div key={idx} style={{
                    background: '#ffffff',
                    border: '1px solid #cbd5e1',
                    borderRadius: 6,
                    padding: 10,
                    display: 'flex',
                    flexDirection: 'column',
                    gap: 6,
                    boxShadow: '0 1px 2px rgba(0,0,0,0.04)',
                  }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <span style={{ fontSize: 10, fontWeight: 800, color: '#460073', textTransform: 'uppercase' }}>
                        Source #{idx + 1}
                      </span>
                      <button
                        type="button"
                        onClick={() => deleteUpstreamSource(idx)}
                        style={{
                          background: '#fee2e2',
                          color: '#dc2626',
                          border: 'none',
                          borderRadius: 4,
                          padding: '3px 6px',
                          cursor: 'pointer',
                        }}
                        title="Remove Source"
                      >
                        <TrashIcon />
                      </button>
                    </div>
                    <div>
                      <span style={{ fontSize: 9.5, color: '#64748b', fontWeight: 600 }}>SYSTEM NAME</span>
                      <input
                        type="text"
                        value={src.system || ''}
                        onChange={e => updateUpstreamSource(idx, 'system', e.target.value)}
                        style={{ ...inputStyle, width: '100%', marginTop: 2, fontWeight: 600 }}
                        placeholder="e.g. Core Banking / IoT Telemetry"
                      />
                    </div>
                    <div style={{ display: 'flex', gap: 6 }}>
                      <div style={{ flex: 1 }}>
                        <span style={{ fontSize: 9.5, color: '#64748b', fontWeight: 600 }}>LAYER</span>
                        <input
                          type="text"
                          value={src.layer || 'BRONZE'}
                          onChange={e => updateUpstreamSource(idx, 'layer', e.target.value)}
                          style={{ ...inputStyle, width: '100%', marginTop: 2 }}
                        />
                      </div>
                      <div style={{ flex: 1 }}>
                        <span style={{ fontSize: 9.5, color: '#64748b', fontWeight: 600 }}>TYPE</span>
                        <select
                          value={src.type || 'CDC_STREAM'}
                          onChange={e => updateUpstreamSource(idx, 'type', e.target.value)}
                          style={{ ...inputStyle, width: '100%', marginTop: 2 }}
                        >
                          <option value="CDC_STREAM">CDC_STREAM</option>
                          <option value="BATCH_DELTA">BATCH_DELTA</option>
                          <option value="API_EVENT">API_EVENT</option>
                          <option value="FILE_EXTRACT">FILE_EXTRACT</option>
                        </select>
                      </div>
                      <div style={{ flex: 1 }}>
                        <span style={{ fontSize: 9.5, color: '#64748b', fontWeight: 600 }}>FREQUENCY</span>
                        <select
                          value={src.frequency || 'HOURLY'}
                          onChange={e => updateUpstreamSource(idx, 'frequency', e.target.value)}
                          style={{ ...inputStyle, width: '100%', marginTop: 2 }}
                        >
                          <option value="REALTIME">REALTIME</option>
                          <option value="HOURLY">HOURLY</option>
                          <option value="DAILY">DAILY</option>
                          <option value="BATCH">BATCH</option>
                        </select>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* Entity & Column Lineage Mapping */}
            <div style={{ background: '#f8fafc', border: '1px solid #e2e8f0', borderRadius: 8, padding: 14 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
                <div>
                  <h4 style={{ margin: 0, fontSize: 13, fontWeight: 700, color: '#1e293b' }}>
                    2. Conformed Entity & Column Transformations (Source → Target Lineage)
                  </h4>
                  <div style={{ fontSize: 11, color: '#64748b', marginTop: 2 }}>
                    Attribute-level mapping, cleaning logic, type conformance, and business hashing rules
                  </div>
                </div>
                <button
                  type="button"
                  onClick={addEntityLineage}
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
                  <PlusIcon /> Add Entity Lineage
                </button>
              </div>

              {(contract.lineage?.entity_lineage || []).length === 0 ? (
                <div style={{ textAlign: 'center', padding: '24px 0', color: '#64748b', fontSize: 12 }}>
                  No entity lineage defined yet. Click "Add Entity Lineage" to link Gold Tables to upstream Bronze sources.
                </div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                  {(contract.lineage?.entity_lineage || []).map((ent, tableIdx) => (
                    <div key={tableIdx} style={{
                      background: '#ffffff',
                      border: '1px solid #cbd5e1',
                      borderRadius: 6,
                      padding: 12,
                      boxShadow: '0 1px 2px rgba(0,0,0,0.03)',
                    }}>
                      {/* Entity Header */}
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10, flexWrap: 'wrap', gap: 8 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                          <span style={{ fontSize: 11, fontWeight: 800, color: '#460073', background: '#EDE9FE', padding: '2px 8px', borderRadius: 4 }}>
                            GOLD TARGET:
                          </span>
                          <input
                            type="text"
                            value={ent.target_table || ''}
                            onChange={e => updateEntityLineage(tableIdx, 'target_table', e.target.value)}
                            style={{ ...inputStyle, fontWeight: 700, color: '#0f172a', width: 170 }}
                            placeholder="Target table"
                          />
                          <span style={{ fontSize: 12, fontWeight: 700, color: '#94a3b8' }}>←</span>
                          <span style={{ fontSize: 11, fontWeight: 800, color: '#0369a1', background: '#e0f2fe', padding: '2px 8px', borderRadius: 4 }}>
                            BRONZE SOURCE:
                          </span>
                          <input
                            type="text"
                            value={ent.source_table || ''}
                            onChange={e => updateEntityLineage(tableIdx, 'source_table', e.target.value)}
                            style={{ ...inputStyle, fontWeight: 600, color: '#0369a1', width: 170 }}
                            placeholder="Source table"
                          />
                          {ent.join_key !== undefined && (
                            <span style={{ fontSize: 10, color: '#64748b' }}>
                              Join Key: <input
                                type="text"
                                value={ent.join_key || ''}
                                onChange={e => updateEntityLineage(tableIdx, 'join_key', e.target.value)}
                                style={{ ...inputStyle, width: 100, fontSize: 10, display: 'inline-block', padding: '2px 6px' }}
                              />
                            </span>
                          )}
                        </div>

                        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                          <button
                            type="button"
                            onClick={() => addColumnLineage(tableIdx)}
                            style={{
                              background: '#f1f5f9',
                              color: '#334155',
                              border: '1px solid #cbd5e1',
                              borderRadius: 4,
                              padding: '4px 8px',
                              fontSize: 10.5,
                              fontWeight: 600,
                              cursor: 'pointer',
                            }}
                          >
                            + Add Column Lineage
                          </button>
                          <button
                            type="button"
                            onClick={() => deleteEntityLineage(tableIdx)}
                            style={{
                              background: '#fee2e2',
                              color: '#dc2626',
                              border: '1px solid #fca5a5',
                              borderRadius: 4,
                              padding: '4px 6px',
                              cursor: 'pointer',
                            }}
                            title="Delete Entity Lineage"
                          >
                            <TrashIcon />
                          </button>
                        </div>
                      </div>

                      {/* Column Lineage Table */}
                      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
                        <thead>
                          <tr style={{ background: '#f8fafc', borderBottom: '1px solid #e2e8f0', textAlign: 'left' }}>
                            <th style={{ padding: '6px 8px', fontWeight: 700, color: '#475569', width: '28%' }}>Target Column (Gold)</th>
                            <th style={{ padding: '6px 8px', fontWeight: 700, color: '#475569', width: '28%' }}>Source Column (Bronze)</th>
                            <th style={{ padding: '6px 8px', fontWeight: 700, color: '#475569' }}>Transformation / Conformance Expression</th>
                            <th style={{ padding: '6px 8px', textAlign: 'center', width: 40 }}></th>
                          </tr>
                        </thead>
                        <tbody>
                          {(ent.column_lineage || []).map((col, colIdx) => (
                            <tr key={colIdx} style={{ borderBottom: '1px solid #f1f5f9' }}>
                              <td style={{ padding: '4px 8px' }}>
                                <input
                                  type="text"
                                  value={col.target_column || ''}
                                  onChange={e => updateColumnLineage(tableIdx, colIdx, 'target_column', e.target.value)}
                                  style={{ ...inputStyle, width: '100%', fontFamily: 'monospace', fontWeight: 600 }}
                                />
                              </td>
                              <td style={{ padding: '4px 8px' }}>
                                <input
                                  type="text"
                                  value={col.source_column || ''}
                                  onChange={e => updateColumnLineage(tableIdx, colIdx, 'source_column', e.target.value)}
                                  style={{ ...inputStyle, width: '100%', fontFamily: 'monospace', color: '#0369a1' }}
                                />
                              </td>
                              <td style={{ padding: '4px 8px' }}>
                                <input
                                  type="text"
                                  value={col.transformation || ''}
                                  onChange={e => updateColumnLineage(tableIdx, colIdx, 'transformation', e.target.value)}
                                  style={{ ...inputStyle, width: '100%', fontFamily: 'monospace', color: '#7500C0', background: '#faf5ff' }}
                                />
                              </td>
                              <td style={{ padding: '4px 8px', textAlign: 'center' }}>
                                <button
                                  type="button"
                                  onClick={() => deleteColumnLineage(tableIdx, colIdx)}
                                  style={{
                                    background: '#fee2e2',
                                    color: '#dc2626',
                                    border: '1px solid #fca5a5',
                                    borderRadius: 4,
                                    padding: '2px 5px',
                                    cursor: 'pointer',
                                  }}
                                  title="Delete column mapping"
                                >
                                  <TrashIcon />
                                </button>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ))}
                </div>
              )}
            </div>
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
                  placeholder="Target Databricks Unity Catalog schema..."
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
                    placeholder="4h lag"
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
                    placeholder="99.95%"
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
          <span>Domain: <strong>{contract.info?.domain || 'Core Banking'}</strong></span>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {savedBadge && (
            <span style={{
              background: '#ECFDF5',
              color: '#065F46',
              border: '1px solid #A7F3D0',
              fontSize: 10.5,
              fontWeight: 700,
              padding: '4px 8px',
              borderRadius: 6,
              display: 'flex',
              alignItems: 'center',
              gap: 4,
            }}>
              <CheckIcon /> Contract Applied &amp; Saved!
            </span>
          )}
          <button
            type="button"
            onClick={handleSave}
            disabled={saving}
            style={{
              background: saving ? '#059669' : '#10B981',
              color: '#ffffff',
              border: 'none',
              borderRadius: 6,
              padding: '6px 14px',
              fontSize: 11,
              fontWeight: 700,
              cursor: saving ? 'wait' : 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: 5,
              opacity: saving ? 0.85 : 1,
            }}
          >
            <SaveIcon /> {saving ? 'Applying & Generating Artifacts...' : 'Apply & Save Contract'}
          </button>
        </div>
      </div>
    </div>
  )
}

