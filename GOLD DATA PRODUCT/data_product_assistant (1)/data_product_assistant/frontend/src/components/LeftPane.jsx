export default function LeftPane() {
  return (
    <aside className="left">
      <div className="left-search-wrap">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
          <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
        </svg>
        <input className="left-search" type="text" placeholder="Search" />
      </div>

      <p className="left-section">CHATS</p>
      <ul className="left-list">
        <li className="left-item active">
          <div className="left-av" style={{ background: '#A100FF' }}>DA</div>
          <div className="left-item-body">
            <span className="left-item-name">Data Product A…</span>
            <span className="left-item-sub">Hi! What are you trying…</span>
          </div>
          <span className="left-badge">1</span>
        </li>
        <li className="left-item">
          <div className="left-av" style={{ background: '#6366f1' }}>AT</div>
          <div className="left-item-body">
            <span className="left-item-name">Analytics team</span>
            <span className="left-item-sub">Q3 review tomorrow</span>
          </div>
        </li>
        <li className="left-item">
          <div className="left-av" style={{ background: '#f59e0b' }}>UP</div>
          <div className="left-item-body">
            <span className="left-item-name">Upstream data</span>
            <span className="left-item-sub">New well data available</span>
          </div>
        </li>
      </ul>

      <p className="left-section">APPS</p>
      <ul className="left-list">
        <li className="left-item">
          <div className="left-app-icon">📊</div>
          <div className="left-item-body">
            <span className="left-item-name">Reports (Looker)</span>
            <span className="left-item-sub">View published data products</span>
          </div>
        </li>
      </ul>
    </aside>
  )
}
