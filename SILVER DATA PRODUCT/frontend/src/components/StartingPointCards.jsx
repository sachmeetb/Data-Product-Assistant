const STARTING_POINTS = [
  {
    key: 'dpi',
    title: 'Find Banking Datasets',
    badge: 'DPI',
    description: 'I have a banking analytics request — discover existing Silver & Gold datasets.',
    agents: 'Requirement Understanding · Domain Discovery · Data Product Engine',
    prompt: 'Help me find banking data — I have a business requirement but not sure what Silver/Gold tables exist.',
  },
  {
    key: 'ddi',
    title: 'Design Banking Silver Schema',
    badge: 'DDI',
    description: 'Provide bank profile & requirement — build BIAN-aligned Silver ER + STTM + Data Specs.',
    agents: 'Bank Profile Agent · Domain Scoper · Silver Product Engine',
    prompt: 'Help me design a Banking Silver Schema — I want to build a canonical Silver model for my bank.',
  },
  {
    key: 'dpb',
    title: 'Generate BigQuery DDL Pipeline',
    badge: 'DPB',
    description: 'Requirements & schema ready — generate and validate executable BigQuery DDL DML.',
    agents: 'Spec Generator · Validator Agent · BigQuery Publisher',
    prompt: 'Just build it — generate the executable BigQuery Silver DDL script and data specifications.',
  },
]

export default function StartingPointCards({ onPick, disabled }) {
  return (
    <div className={`starting-points ${disabled ? 'disabled' : ''}`}>
      <div className="starting-points-header">PICK A STARTING POINT</div>
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
          <div className="starting-point-start">Start here</div>
        </button>
      ))}
    </div>
  )
}
