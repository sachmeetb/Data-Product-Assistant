const STARTING_POINTS = [
  {
    key: 'unstructured_kyc',
    title: 'Unstructured KYC Documents & Contracts (PDF / Scans)',
    badge: 'UNSTRUCTURED · OBJECT TABLE',
    description: 'Ingest customer identity verification PDFs, scanned passports, and loan contracts into BigLake Object Tables.',
    agents: 'Source Scoping · BigLake Object Tables · Dataplex Catalog',
    prompt: 'Help me ingest unstructured KYC verification documents — we have customer ID scans, passports, and loan agreement PDFs in GCS to register as BigLake Object Tables with SHA-256 integrity hashes, OCR text metadata, and 7-year regulatory retention.',
  },
  {
    key: 'streaming_fraud',
    title: 'Real-Time Card Authorization & Fraud Streams (Kafka)',
    badge: 'STREAMING · REALTIME',
    description: 'Sub-5-minute SLA event-driven Kafka and Pub/Sub streams for real-time payment authorization and fraud detection.',
    agents: 'Source Scoper · Micro-Batch Engine · Realtime SLA',
    prompt: 'Help me design a real-time Bronze ingestion stream for payment card authorization and fraud detection events arriving via Kafka / Google Cloud Pub/Sub with sub-5-minute SLA, topic offset lineage, and Iceberg table partitioning.',
  },
  {
    key: 'hogan',
    title: 'Hogan Deposit System (Mainframe Copybooks)',
    badge: 'BATCH · HOGAN',
    description: 'Ingest Hogan Demand Deposit (DD-REC) & CIS (CI-REC) fixed-width records with 7-year Basel III retention.',
    agents: 'Bank Profile · Source Scoping · Bronze Product Engine',
    prompt: 'Help me ingest Hogan Deposit System core banking feeds — we have mainframe COBOL copybooks for Demand Deposits (DD-REC) and Customer Information (CI-REC) to land in Google Data Lake with Apache Iceberg external tables and 7-year regulatory retention.',
  },
  {
    key: 'swift_fps',
    title: 'SWIFT MT/MX & Faster Payments Stream',
    badge: 'STREAMING · PAYMENTS',
    description: 'Real-time UK Faster Payments (FPS) and SWIFT MT103 / pacs.008 clearing feeds with UETR envelope.',
    agents: 'Source Scoper · Spec Generator · Iceberg Engine',
    prompt: 'Design Bronze ingestion for SWIFT MT/MX messages (MT103, pacs.008) and UK Faster Payments (FPS) real-time clearing into Google Cloud Data Lake with 7-year retention and ODCS v2.2 data contracts.',
  },
  {
    key: 'sap',
    title: 'SAP S/4HANA FI-CO General Ledger',
    badge: 'BATCH · SAP ERP',
    description: 'SAP Financial Accounting documents (BKPF, BSEG) with SLT change capture & audit controls.',
    agents: 'Bank Profile · Source Scoping · Validator Agent',
    prompt: 'Help me design a Bronze landing schema for SAP S/4HANA General Ledger (BKPF document header and BSEG line items) with SLT replication metadata, financial audit controls, and 7-year retention.',
  },
  {
    key: 'salesforce',
    title: 'Salesforce CRM & Client KYC Ingestion',
    badge: 'BATCH · CRM',
    description: 'Salesforce customer Accounts and KYC compliance screening events with automated PII tagging.',
    agents: 'Requirement Understanding · Source Scoper · Dataplex Catalog',
    prompt: 'Help me ingest Salesforce CRM customer Accounts and KYC Verification compliance records into Bronze layer with automated PII classification, CDC replay metadata, and Dataplex tag templates.',
  },
  {
    key: 'temenos',
    title: 'Temenos T24 Transact Core Banking Batch',
    badge: 'BATCH · TEMENOS',
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
