export default function BusinessGlossaryCard({ glossary }) {
  const { entries = [], column_count = 0 } = glossary

  return (
    <div className="bg-card">
      <div className="bg-header">
        <span className="bg-title">Business glossary — confirm definitions</span>
        <span className="bg-count-badge">{column_count} column{column_count !== 1 ? 's' : ''}</span>
      </div>

      <p className="bg-subtitle">
        One-line, plain-English description of every field in your dashboard.
        Edit any line, or accept all to lock the glossary.
      </p>

      <hr className="bg-divider" />

      {entries.map((entry, i) => (
        <div key={i} className="bg-entry">
          <div className="bg-entry-header">
            <span className="bg-entry-name">{entry.name}</span>
            <span className={`bg-type-badge bg-type-${entry.type_color}`}>{entry.type_label}</span>
            <span className="bg-sql-type">{entry.sql_type}</span>
          </div>
          <p className="bg-entry-desc">{entry.description}</p>
        </div>
      ))}

      <p className="bg-footer">
        These descriptions are saved to the data product&apos;s{' '}
        <strong>business glossary</strong> and shown in Power BI tooltips and Unity Catalog.
      </p>
    </div>
  )
}
