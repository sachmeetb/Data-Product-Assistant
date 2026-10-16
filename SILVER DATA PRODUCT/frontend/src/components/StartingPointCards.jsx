const STARTING_POINTS = [
  {
    key: 'explore',
    title: 'Explore Multi-Industry Domains',
    badge: 'CATALOG',
    description: 'Explore canonical domains for Industrial & Water (Ecolab), Banking, Retail, Automotive, & Life Sciences.',
    agents: 'Multi-Industry Catalog · Domain Discovery · Requirement Understanding',
    prompt: 'Help me explore available domains across industries — I want to build a conformed Silver data product.',
  },
  {
    key: 'ddi',
    title: 'Design Conformed Silver Schema',
    badge: 'SCHEMA',
    description: 'Select industry, subtype, and domain — generate conformed Silver ER model, ODCS contract, and lineage.',
    agents: 'Industry Classifier · Domain Scoper · Silver Product Engine',
    prompt: 'Help me design a Conformed Silver Schema — I want to align canonical entities with upstream bronze lineage.',
  },
  {
    key: 'dpb',
    title: 'Generate BigQuery DDL & Contract',
    badge: 'CONTRACT',
    description: 'Requirement ready — generate production BigQuery Silver DDL, ODCS contract, and source-to-target lineage.',
    agents: 'Spec Generator · Lineage Validator · BigQuery Publisher',
    prompt: 'Generate the complete Conformed Silver Data Contract, BigQuery DDL, and lineage specifications.',
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
