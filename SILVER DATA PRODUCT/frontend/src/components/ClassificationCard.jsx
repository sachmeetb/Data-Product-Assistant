export default function ClassificationCard({ view }) {
  const { use_case_label, schema_label, signals = [], rationale } = view

  return (
    <div className="cls-card">
      <div className="cls-header">
        <span className="cls-header-label">USE CASE CLASSIFIED</span>
        <span className="cls-use-case-type">{use_case_label}</span>
        <span className="cls-schema-label">Routing → {schema_label}</span>
      </div>

      {signals.length > 0 && (
        <div className="cls-body">
          <div className="cls-signals-label">MATCHED SIGNALS</div>
          <div className="cls-signals">
            {signals.map((s, i) => (
              <span key={i} className="cls-signal-pill">{s}</span>
            ))}
          </div>
          {rationale && <p className="cls-rationale">{rationale}</p>}
        </div>
      )}
    </div>
  )
}
