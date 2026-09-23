import { BP } from '../theme'
import { MessageSquare, Users, Calendar, FileText } from '../icons'

// Left-most icon rail — ported from the prototype.
export default function NavRail() {
  return (
    <div className="flex flex-col items-center pt-3 gap-2 shrink-0" style={{ width: 56, background: BP.greenDeep }}>
      <div className="flex flex-col items-center gap-1">
        <div className="flex items-center justify-center" style={{ width: 36, height: 36, borderRadius: 8, background: BP.green, color: 'white' }}>
          <MessageSquare size={18} />
        </div>
        {[Users, Calendar, FileText].map((Icon, i) => (
          <div key={i} className="flex items-center justify-center cursor-pointer"
            style={{ width: 36, height: 36, borderRadius: 8, color: 'rgba(255,255,255,0.78)' }}>
            <Icon size={18} />
          </div>
        ))}
      </div>
      <div style={{ flex: 1 }} />
      <div className="t-10 font-semibold tracking-wider mb-3"
        style={{ color: 'rgba(255,255,255,0.6)', writingMode: 'vertical-rl', transform: 'rotate(180deg)' }}>
        Accenture · Agentic Platform
      </div>
    </div>
  )
}
