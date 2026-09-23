import { BP, PHASES } from '../theme'
import { Search, CheckCircle2, Loader2, BarChart3, Database, Layers, Rocket } from '../icons'

const PHASE_ICONS = { Search, Layers, Rocket }

function Avatar({ code, color }) {
  return (
    <div className="flex items-center justify-center font-semibold text-white shrink-0"
      style={{ width: 32, height: 32, borderRadius: '50%', background: color, fontSize: 12, letterSpacing: 0.3 }}>
      {code}
    </div>
  )
}

// Chats list + phase Progress stepper + Apps — ported from the prototype.
// `activePhase` drives the Progress highlight; before any phase is active it
// shows all three phases idle.
export default function ChatSidebar({ activePhase, useCaseSubtitle = 'Daily campaign summary' }) {
  const phaseIdx = PHASES.findIndex(x => x.key === activePhase)

  return (
    <div className="flex flex-col shrink-0 border-r" style={{ width: 268, background: BP.greenDeeper, borderColor: BP.greenDeep }}>
      <div className="px-3 pt-3 pb-2">
        <div className="flex items-center gap-2 px-3 rounded-md" style={{ background: 'rgba(255,255,255,0.08)', height: 34 }}>
          <Search size={14} style={{ color: 'rgba(255,255,255,0.6)' }} />
          <span className="text-sm" style={{ color: 'rgba(255,255,255,0.55)' }}>Search</span>
        </div>
      </div>

      <div className="px-4 pt-3 pb-1 t-11 font-bold tracking-widest" style={{ color: 'rgba(255,255,255,0.55)' }}>CHATS</div>

      <div className="px-2">
        <div className="flex gap-2.5 items-center px-2 py-2 rounded-md" style={{ background: BP.green }}>
          <Avatar code="DA" color="rgba(255,255,255,0.22)" />
          <div className="flex-1 min-w-0">
            <div className="t-13 font-semibold text-white truncate">Data Product A…</div>
            <div className="t-11 truncate" style={{ color: 'rgba(255,255,255,0.78)' }}>{useCaseSubtitle}</div>
          </div>
          <span className="t-10 font-bold px-1.5 rounded-full text-white shrink-0" style={{ background: 'rgba(255,255,255,0.25)', lineHeight: '16px' }}>●</span>
        </div>

        {[
          { code: 'AT', title: 'Analytics team', sub: 'Q3 review tomorrow' },
          { code: 'UP', title: 'Upstream data', sub: 'New well data available' },
        ].map(c => (
          <div key={c.code} className="flex gap-2.5 items-center px-2 py-2 rounded-md cursor-pointer">
            <Avatar code={c.code} color="rgba(255,255,255,0.18)" />
            <div className="flex-1 min-w-0">
              <div className="t-13 font-medium truncate" style={{ color: 'rgba(255,255,255,0.92)' }}>{c.title}</div>
              <div className="t-11 truncate" style={{ color: 'rgba(255,255,255,0.55)' }}>{c.sub}</div>
            </div>
          </div>
        ))}
      </div>

      <div className="px-4 pt-4 pb-1 t-11 font-bold tracking-widest" style={{ color: 'rgba(255,255,255,0.55)' }}>PROGRESS</div>
      <div className="px-3 pb-3 space-y-1.5">
        {PHASES.map((p, i) => {
          const active = p.key === activePhase
          const done = phaseIdx >= 0 && i < phaseIdx
          const Icon = PHASE_ICONS[p.icon] || Search
          return (
            <div key={p.key} className="flex items-center gap-2.5 px-2 py-2">
              <div className="flex items-center justify-center" style={{
                width: 26, height: 26, borderRadius: 6,
                background: done ? BP.green : (active ? 'rgba(255,255,255,0.16)' : 'rgba(255,255,255,0.06)'),
              }}>
                {done ? <CheckCircle2 size={14} color="white" /> : <Icon size={13} color="white" />}
              </div>
              <div className="flex-1 min-w-0">
                <div className="t-125 truncate" style={{ color: active ? 'white' : 'rgba(255,255,255,0.65)', fontWeight: active ? 700 : 500 }}>
                  {p.label}
                </div>
              </div>
              {active && <Loader2 size={12} className="animate-spin" style={{ color: 'rgba(255,255,255,0.7)' }} />}
            </div>
          )
        })}
      </div>

      <div className="px-4 pt-3 pb-1 t-11 font-bold tracking-widest" style={{ color: 'rgba(255,255,255,0.55)' }}>APPS</div>
      <div className="px-3 space-y-2">
        <div className="flex items-center gap-2.5 px-2 py-1.5 rounded-md cursor-pointer">
          <BarChart3 size={16} style={{ color: BP.yellow }} />
          <div>
            <div className="t-12 font-medium text-white">Reports (Looker)</div>
            <div className="t-10" style={{ color: 'rgba(255,255,255,0.55)' }}>View published products</div>
          </div>
        </div>
        <div className="flex items-center gap-2.5 px-2 py-1.5 rounded-md cursor-pointer">
          <Database size={16} style={{ color: BP.yellow }} />
          <div>
            <div className="t-12 font-medium text-white">Data catalog</div>
            <div className="t-10" style={{ color: 'rgba(255,255,255,0.55)' }}>Browse existing assets</div>
          </div>
        </div>
      </div>

      <div style={{ flex: 1 }} />
    </div>
  )
}
