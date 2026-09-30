const STARTING_POINTS = [
  {
    key: 'hogan',
    title: 'Hogan Deposit System (Mainframe Copybooks)',
    badge: 'HOGAN',
    description: 'Ingest Hogan Demand Deposit (DD-REC) & CIS (CI-REC) fixed-width records with 7-year Basel III retention.',
    agents: 'Bank Profile · Source Scoping · Bronze Product Engine',
    prompt: 'Help me ingest Hogan Deposit System core banking feeds — we have mainframe COBOL copybooks for Demand Deposits (DD-REC) and Customer Information (CI-REC) to land in Google Data Lake with Apache Iceberg external tables and 7-year regulatory retention.',
  },
  {
    key: 'swift_fps',
    title: 'SWIFT MT/MX & Faster Payments Stream',
    badge: 'PAYMENTS',
    description: 'Real-time UK Faster Payments (FPS) and SWIFT MT103 / pacs.008 clearing feeds with UETR envelope.',
    agents: 'Source Scoper · Spec Generator · Iceberg Engine',
    prompt: 'Design Bronze ingestion for SWIFT MT/MX messages (MT103, pacs.008) and UK Faster Payments (FPS) real-time clearing into Google Cloud Data Lake with 7-year retention and ODCS v2.2 data contracts.',
  },
  {
    key: 'sap',
    title: 'SAP S/4HANA FI-CO General Ledger',
    badge: 'SAP ERP',
    description: 'SAP Financial Accounting documents (BKPF, BSEG) with SLT change capture & audit controls.',
    agents: 'Bank Profile · Source Scoping · Validator Agent',
    prompt: 'Help me design a Bronze landing schema for SAP S/4HANA General Ledger (BKPF document header and BSEG line items) with SLT replication metadata, financial audit controls, and 7-year retention.',
  },
  {
    key: 'salesforce',
    title: 'Salesforce CRM & Client KYC Ingestion',
    badge: 'CRM',
    description: 'Salesforce customer Accounts and KYC compliance screening events with automated PII tagging.',
    agents: 'Requirement Understanding · Source Scoper · Dataplex Catalog',
    prompt: 'Help me ingest Salesforce CRM customer Accounts and KYC Verification compliance records into Bronze layer with automated PII classification, CDC replay metadata, and Dataplex tag templates.',
  },
  {
    key: 'temenos',
    title: 'Temenos T24 Transact Core Banking Batch',
    badge: 'TEMENOS',
    description: 'Temenos T24 core banking daily batch extracts (CUSTOMER, ACCOUNT, FT) into BigLake Iceberg.',
    agents: 'Bank Profile · Bronze Engine · Spec Generator',
    prompt: 'Help me design a Banking Bronze Schema for Temenos T24 Transact daily batch files with BigLake Apache Iceberg external tables, 4-field ingestion envelopes, and ODCS contract.',
  },
]

export default function StartingPointCards({ onPick, disabled }) {
  return (
    <div className={`starting-points ${disabled ? 'disabled' : ''}`}>
      <div className="starting-points-header">POPULAR BRONZE INGESTION PATTERNS</div>
      {STARTING_POINTS.map(sp => (
        <button
          key={sp.key}
          className="starting-point-card"
          onClick={() => !disabled && onPick(sp.prompt)}
          disabled={disabled}
        >
          <div className="starting-point-icon" />
          <div className="starting-point-body">
            <div className="starting-point-title-row">
              <span className="starting-point-title">{sp.title}</span>
              <span className="starting-point-badge">{sp.badge}</span>
            </div>
            <div className="starting-point-desc">{sp.description}</div>
            <div className="starting-point-agents">Agents: {sp.agents}</div>
          </div>
          <div className="starting-point-start">Select</div>
        </button>
      ))}
    </div>
  )
}
