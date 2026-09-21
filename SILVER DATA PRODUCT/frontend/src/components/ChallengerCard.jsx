const VERDICT_META = {
  clean:    { label: '✓ CLEAN',    cls: 'ch-badge-clean' },
  concerns: { label: '⚠ CONCERNS', cls: 'ch-badge-concerns' },
  blockers: { label: '✗ BLOCKERS', cls: 'ch-badge-blockers' },
}

export default function ChallengerCard({ view }) {
  const { verdict = 'concerns', checks = [], summary = '' } = view
  const meta = VERDICT_META[verdict] ?? VERDICT_META.concerns

  return (
    <div className="ch-card">
      <div className="ch-header">
        <span className="ch-header-label">CHALLENGER REVIEW</span>
        <span className={`ch-badge ${meta.cls}`}>{meta.label}</span>
      </div>

      {checks.length > 0 && (
        <ul className="ch-checks">
          {checks.map((c, i) => (
            <li key={i} className={`ch-check ${c.passed ? 'ch-check-pass' : 'ch-check-fail'}`}>
              <span className="ch-check-icon">{c.passed ? '✓' : '✗'}</span>
              <span className="ch-check-label">{c.label}</span>
            </li>
          ))}
        </ul>
      )}

      {summary && <p className="ch-summary">{summary}</p>}
    </div>
  )
}
