import { BP } from '../theme'
import { Search, Layers, Rocket, ChevronRight, Sparkles } from '../icons'

const STARTING_POINTS = [
  {
    key: 'dpi',
    title: 'Help me find the data',
    builds: 'Build 1',
    icon: 'Search',
    subtitle: 'I have a question — not sure what data exists.',
    agentList: 'Requirements · Discovery · Challenger',
    primary: true,
    prompt: 'Help me find the data — I have a question but not sure what data exists.',
  },
  {
    key: 'ddi',
    title: 'Help me design it',
    builds: 'Build 2',
    icon: 'Layers',
    subtitle: 'Requirements ready · need schemas & contracts.',
    agentList: 'Schema Strategist · Transformers · Contracts',
    prompt: 'Help me design it — I have a Discovery output JSON.',
  },
  {
    key: 'dpb',
    title: 'Just build it',
    builds: 'Build 3',
    icon: 'Rocket',
    subtitle: 'Design / STTM ready · generate the pipeline.',
    agentList: 'Pipeline · Test · Publisher',
    prompt: 'Just build it — design/STTM is ready, generate the pipeline.',
  },
]

const ICONS = { Search, Layers, Rocket }

// Opening pathway selector — ported from the prototype's TriageCard. Keeps the
// live onPick(prompt) wiring; `disabled` greys it out once a path is chosen.
export default function StartingPointCards({ onPick, disabled }) {
  return (
    <div className="mt-2.5 rounded-xl overflow-hidden" style={{ border: `1px solid ${BP.greenLine}`, background: 'white', maxWidth: 620, opacity: disabled ? 0.6 : 1 }}>
      <div className="px-4 py-2.5" style={{ background: `linear-gradient(135deg, ${BP.greenDeep} 0%, ${BP.green} 100%)` }}>
        <div className="flex items-center gap-2 text-white">
          <Sparkles size={13} color={BP.yellow} />
          <div className="t-115 font-bold tracking-wide">PICK A STARTING POINT</div>
        </div>
      </div>
      <div className="p-3 space-y-2">
        {STARTING_POINTS.map(o => {
          const Icon = ICONS[o.icon] || Search
          return (
            <button
              key={o.key}
              onClick={() => !disabled && onPick(o.prompt)}
              disabled={disabled}
              className="w-full text-left rounded-lg"
              style={{
                background: o.primary ? BP.greenLight : 'white',
                border: `1px solid ${o.primary ? BP.green : BP.border}`,
                padding: '12px 14px', cursor: disabled ? 'not-allowed' : 'pointer',
              }}
            >
              <div className="flex items-start gap-3">
                <div className="flex items-center justify-center shrink-0" style={{
                  width: 36, height: 36, borderRadius: 8,
                  background: o.primary ? BP.green : BP.greenLight,
                  color: o.primary ? 'white' : BP.greenDark,
                }}>
                  <Icon size={17} />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-baseline gap-2 flex-wrap">
                    <div className="t-135 font-bold" style={{ color: BP.text }}>{o.title}</div>
                    <span className="t-105 px-1.5 py-0.5 rounded-full font-bold" style={{
                      background: o.primary ? BP.green : BP.greenLight,
                      color: o.primary ? 'white' : BP.greenDark,
                      border: `1px solid ${o.primary ? BP.green : BP.greenLine}`,
                    }}>{o.builds}</span>
                  </div>
                  <div className="t-12 mt-1" style={{ color: BP.textMuted, lineHeight: 1.5 }}>{o.subtitle}</div>
                  <div className="t-105 mt-1.5 font-semibold" style={{ color: BP.greenDark }}>Agents: {o.agentList}</div>
                </div>
                <div className="t-12 font-semibold flex items-center gap-1 shrink-0" style={{ color: BP.greenDark }}>
                  Start here <ChevronRight size={13} />
                </div>
              </div>
            </button>
          )
        })}
      </div>
    </div>
  )
}
