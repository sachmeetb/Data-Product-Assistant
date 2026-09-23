import { BP } from '../theme'
import { Sparkles } from '../icons'

// Ported from the prototype ClassificationCard. Backend `view` shape:
//   { use_case_label, schema_label, signals[], rationale, confidence? }
export default function ClassificationCard({ view }) {
  const { use_case_label, schema_label, signals = [], rationale, confidence } = view

  return (
    <div className="mt-2.5 rounded-xl overflow-hidden" style={{ border: `1px solid ${BP.greenLine}`, background: 'white', maxWidth: 460 }}>
      <div className="px-4 py-3" style={{ background: 'linear-gradient(135deg, #A100FF 0%, #7800C4 100%)' }}>
        <div className="flex items-center justify-between text-white">
          <div className="flex items-center gap-2">
            <Sparkles size={15} />
            <div className="t-13 font-bold tracking-wide">USE CASE CLASSIFIED</div>
          </div>
          {confidence != null && <div className="t-20 font-bold">{confidence}%</div>}
        </div>
        <div className="mt-1 t-18 font-bold text-white">{use_case_label}</div>
        {schema_label && (
          <div className="t-115" style={{ color: 'rgba(255,255,255,0.85)' }}>Routing → {schema_label}</div>
        )}
      </div>
      {(signals.length > 0 || rationale) && (
        <div className="px-4 py-3">
          {signals.length > 0 && (
            <>
              <div className="t-11 font-bold tracking-wider mb-1.5" style={{ color: BP.textMuted }}>MATCHED SIGNALS</div>
              <div className="flex flex-wrap gap-1.5 mb-3">
                {signals.map((s, i) => (
                  <span key={i} className="t-11 px-2 py-0.5 rounded-full font-semibold"
                    style={{ background: BP.greenLight, color: BP.greenDark, border: `1px solid ${BP.greenLine}` }}>{s}</span>
                ))}
              </div>
            </>
          )}
          {rationale && <div className="t-12" style={{ color: BP.textMuted, lineHeight: 1.5 }}>{rationale}</div>}
        </div>
      )}
    </div>
  )
}
