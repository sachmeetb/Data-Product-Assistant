import { BP } from '../theme'
import { BookOpen } from '../icons'

const ROLE_STYLE = {
  green:  { bg: BP.greenLight, color: BP.greenDark, line: BP.greenLine },
  yellow: { bg: BP.warnBg,     color: '#9A6A00',    line: BP.warnLine },
  blue:   { bg: '#F1F4FF',     color: '#3949AB',    line: '#D5DCF4' },
}

// Ported from the prototype DefinitionsCard. Backend `glossary` shape:
//   { entries:[{name, description, type_label, type_color, sql_type}], column_count }
export default function BusinessGlossaryCard({ glossary }) {
  const { entries = [], column_count = 0 } = glossary

  return (
    <div className="mt-2.5 rounded-xl overflow-hidden" style={{ border: `1px solid ${BP.greenLine}`, background: 'white', maxWidth: 560 }}>
      <div className="flex items-center justify-between px-4 py-2.5" style={{ background: BP.greenLight, borderBottom: `1px solid ${BP.greenLine}` }}>
        <div className="flex items-center gap-2 t-13 font-bold" style={{ color: BP.text }}>
          <BookOpen size={13} color={BP.greenDark} />
          Business glossary — confirm definitions
        </div>
        <div className="t-105 font-bold px-2 py-0.5 rounded-full" style={{ background: BP.green, color: 'white' }}>
          {column_count} column{column_count !== 1 ? 's' : ''}
        </div>
      </div>

      <div className="px-4 py-2 t-115" style={{ color: BP.textMuted, background: '#FAF7FD', borderBottom: `1px dashed ${BP.border}`, lineHeight: 1.5 }}>
        One-line, plain-English description of every field in your dashboard. Edit any line, or accept all to lock the glossary.
      </div>

      <div className="px-4 py-3 space-y-2.5">
        {entries.map((e, i) => {
          const rs = ROLE_STYLE[e.type_color] || ROLE_STYLE.blue
          return (
            <div key={i} className="rounded-lg" style={{ border: `1px solid ${BP.border}`, background: '#FAF7FD', padding: '10px 12px' }}>
              <div className="flex items-baseline gap-2 flex-wrap mb-1">
                <span className="t-125 font-bold" style={{ color: BP.text, fontFamily: "ui-monospace, 'SF Mono', monospace" }}>{e.name}</span>
                {e.type_label && (
                  <span className="t-105 font-bold px-1.5 py-0.5 rounded-full" style={{ background: rs.bg, color: rs.color, border: `1px solid ${rs.line}` }}>{e.type_label}</span>
                )}
                {e.sql_type && (
                  <span className="t-105 font-semibold px-1.5 py-0.5 rounded" style={{ background: 'white', color: BP.textMuted, border: `1px solid ${BP.border}`, fontFamily: 'ui-monospace, monospace' }}>{e.sql_type}</span>
                )}
              </div>
              <div className="t-12" style={{ color: BP.text, lineHeight: 1.55 }}>{e.description}</div>
            </div>
          )
        })}
      </div>

      <div className="flex items-center justify-between px-4 py-2" style={{ background: '#FAF7FD', borderTop: `1px solid ${BP.border}` }}>
        <div className="t-105" style={{ color: BP.textMuted }}>
          These descriptions are saved to the data product's <b style={{ color: BP.greenDark }}>business glossary</b> and shown in Looker Studio and Knowledge Catalog.
        </div>
      </div>
    </div>
  )
}
