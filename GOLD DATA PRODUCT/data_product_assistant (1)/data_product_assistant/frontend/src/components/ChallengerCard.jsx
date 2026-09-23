import { BP } from '../theme'
import { Shield, CheckCircle2, AlertTriangle } from '../icons'

const VERDICT_META = {
  clean:    { label: 'CLEAN',    color: BP.green },
  concerns: { label: 'CONCERNS', color: BP.amber },
  blockers: { label: 'BLOCKERS', color: '#B91C1C' },
}

// Ported from the prototype ChallengerCard. Backend `view` shape:
//   { verdict: 'clean'|'concerns'|'blockers', checks:[{label, passed}], summary }
export default function ChallengerCard({ view }) {
  const { verdict = 'concerns', checks = [], summary = '' } = view
  const meta = VERDICT_META[verdict] ?? VERDICT_META.concerns

  return (
    <div className="mt-2.5 rounded-xl overflow-hidden" style={{ border: `1px solid ${BP.greenLine}`, background: 'white', maxWidth: 460 }}>
      <div className="flex items-center justify-between px-4 py-2.5" style={{ background: BP.greenLight, borderBottom: `1px solid ${BP.greenLine}` }}>
        <div className="flex items-center gap-2 t-125 font-bold" style={{ color: BP.greenDark }}>
          <Shield size={14} /> CHALLENGER REVIEW
        </div>
        <div className="t-11 font-bold px-2.5 py-0.5 rounded-full" style={{ background: meta.color, color: 'white' }}>
          {verdict === 'clean' ? '✓' : verdict === 'blockers' ? '✗' : '⚠'} {meta.label}
        </div>
      </div>
      <div className="px-4 py-3 space-y-1.5">
        {checks.map((c, i) => (
          <div key={i} className="flex items-center gap-2 t-125">
            {c.passed
              ? <CheckCircle2 size={14} color={BP.green} />
              : <AlertTriangle size={14} color={BP.amber} />}
            <span style={{ color: c.passed ? BP.text : BP.textMuted }}>{c.label}</span>
          </div>
        ))}
        {summary && (
          <div className="pt-2 mt-1 t-12" style={{ color: BP.textMuted, borderTop: `1px dashed ${BP.border}`, lineHeight: 1.5 }}>
            {summary}
          </div>
        )}
      </div>
    </div>
  )
}
