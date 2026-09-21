export default function LeftPane() {
  return (
    <aside className="left">
      <div className="left-search-wrap">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
          <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
        </svg>
        <input className="left-search" type="text" placeholder="Search" />
      </div>

      <p className="left-section">DATA DOMAIN CHATS</p>
      <ul className="left-list">
        <li className="left-item active">
          <div className="left-av" style={{ background: '#7500C0' }}>DA</div>
          <div className="left-item-body">
            <span className="left-item-name">DATA DOMAIN SILVER AGENT</span>
            <span className="left-item-sub">Domain Schemas &amp; DDL…</span>
          </div>
          <span className="left-badge">1</span>
        </li>
      </ul>
    </aside>
  )
}
