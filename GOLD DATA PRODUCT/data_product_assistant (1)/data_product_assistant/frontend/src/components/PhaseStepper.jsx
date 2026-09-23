import { Fragment } from 'react'
import { BP, PHASES } from '../theme'
import { CheckCircle2, ChevronRight } from '../icons'

// Compact phase stepper for the demo header — ported from the prototype.
export default function PhaseStepper({ activePhase }) {
  const idx = PHASES.findIndex(p => p.key === activePhase)
  return (
    <div className="flex items-center gap-1.5">
      {PHASES.map((p, i) => {
        const active = i === idx
        const done = i < idx
        return (
          <Fragment key={p.key}>
            <div className="flex items-center gap-1.5 px-2 py-1 rounded-md t-11 font-semibold" style={{
              background: active ? BP.green : (done ? BP.greenLight : 'transparent'),
              color: active ? 'white' : (done ? BP.greenDark : BP.textMuted),
              border: !active && !done ? `1px solid ${BP.border}` : 'none',
            }}>
              <span>{p.label}</span>
              {done && <CheckCircle2 size={11} />}
            </div>
            {i < PHASES.length - 1 && <ChevronRight size={11} style={{ color: BP.textSubtle }} />}
          </Fragment>
        )
      })}
    </div>
  )
}
