// theme.js — shared design tokens ported verbatim from the prototype
// (Accenture Data Product Assistant 2.html). Single source of truth for the
// brand palette, phase list, and per-agent avatar colours so every restyled
// component stays consistent with the prototype.

// ─── Accenture brand palette (BP) ─────────────────────────────────────────────
export const BP = {
  green:        '#A100FF',   // Accenture primary purple
  greenDark:    '#7800C4',
  greenDeep:    '#460073',   // sidebar / nav rail
  greenDeeper:  '#2E004D',
  greenDeepest: '#1A0033',
  greenLight:   '#F3E6FF',
  greenSoft:    '#E0C2FF',
  greenLine:    '#C97EFF',
  yellow:       '#FF6B00',   // Accenture accent orange
  amber:        '#E05A00',
  text:         '#0F0A14',
  textMuted:    '#5C5066',
  textSubtle:   '#8A8091',
  bg:           '#F7F4FA',
  panel:        '#FFFFFF',
  border:       '#E2DCE8',
  warnBg:       '#FFF3E5',
  warnLine:     '#FFD4A0',
}

// ─── Three delivery phases (drives the NavRail + Progress + PhaseStepper) ──────
export const PHASES = [
  { key: 'dpi',      label: 'Data Product Identifier', builds: 'Build 1', icon: 'Search' },
  { key: 'designer', label: 'Data Designer',           builds: 'Build 2', icon: 'Layers' },
  { key: 'builder',  label: 'Data Product Builder',    builds: 'Build 3', icon: 'Rocket' },
]

// ─── Per-agent avatar colour + label ──────────────────────────────────────────
// The prototype keys this by short code (RA/UC/DD/…). The live backend sends a
// human agent NAME string instead, so we resolve by name below.
export const AGENT_META = {
  RA: { color: '#7800C4', label: 'Requirements Agent' },
  UC: { color: '#9B30E0', label: 'Use Case Determinator' },
  DD: { color: '#A100FF', label: 'Data Discovery Agent' },
  CH: { color: '#5C00B8', label: 'Challenger Agent' },
  GM: { color: '#B26A00', label: 'Gold Model Designer' },
  GS: { color: '#8A5A00', label: 'Gold STTM Generator' },
  MD: { color: '#5F4B8B', label: 'Metadata Generator' },
  DA: { color: BP.green,  label: 'Data Product Assistant' },
  OR: { color: BP.greenDeep, label: 'Orchestrator' },
}

// Map a backend agent name → { code, color, label }. Falls back to the DA
// (assistant) styling for anything unrecognised.
export function agentMeta(agentName) {
  const n = (agentName || '').toLowerCase()
  if (n.includes('requirement'))                 return { code: 'RA', ...AGENT_META.RA }
  if (n.includes('use case') || n.includes('classif') || n.includes('determinator'))
                                                 return { code: 'UC', ...AGENT_META.UC }
  if (n.includes('discovery'))                   return { code: 'DD', ...AGENT_META.DD }
  if (n.includes('challenger'))                  return { code: 'CH', ...AGENT_META.CH }
  if (n.includes('gold model') || n.includes('er '))  return { code: 'GM', ...AGENT_META.GM }
  if (n.includes('sttm'))                        return { code: 'GS', ...AGENT_META.GS }
  if (n.includes('metadata'))                    return { code: 'MD', ...AGENT_META.MD }
  if (n.includes('orchestrator'))                return { code: 'OR', ...AGENT_META.OR }
  return { code: 'DA', ...AGENT_META.DA }
}

// Two-letter avatar initials for a backend agent name.
export function agentInitials(agentName) {
  if (!agentName) return 'DA'
  const words = agentName.trim().split(/\s+/)
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase()
  return agentName.slice(0, 2).toUpperCase()
}

// Derive the active delivery phase from the backend `current_step`.
//
// IMPORTANT: the backend prefixes several *design* and *build* steps with
// `dpi_` (e.g. dpi_review_er, dpi_review_gold_sttm, dpi_review_silver_xform,
// dpi_review_pg). A naive `startsWith('dpi_')` check therefore pinned every one
// of them to phase 1 and the Progress stepper never advanced. So we classify by
// the step's SEMANTIC suffix first (build → design → identify), and only fall
// back to the prefix. We also avoid a bare `includes('er')`, which wrongly
// matched "challeng-ER" / "gen-ER-ator".
export function phaseForStep(step) {
  if (!step) return null
  const s = step.toLowerCase()

  // Build phase — pipeline / publish / test / deploy / approval / done.
  // Covers dpi_review_pg, ddi_* pipeline steps, awaiting_(test_)approval, complete.
  if (
    s.includes('pipeline') || s.includes('_pg') || s.endsWith('pg') ||
    s.includes('publish') || s.includes('test') || s.includes('deploy') ||
    s.includes('approval') || s.startsWith('dpb') || s === 'complete'
  ) return 'builder'

  // Design phase — ER model / STTM / transform / gold-model / silver.
  // Covers dpi_review_er, dpi_review_gold_sttm, dpi_review_silver_xform, ddi_*.
  if (
    s.startsWith('ddi_') || s.includes('review_er') || s.includes('sttm') ||
    s.includes('xform') || s.includes('transform') || s.includes('design') ||
    s.includes('gold_model') || s.includes('silver')
  ) return 'designer'

  // Identify phase — everything else DPI (initial, clarifying, confirm, gate,
  // challenger review, classification, discovery).
  return 'dpi'
}
