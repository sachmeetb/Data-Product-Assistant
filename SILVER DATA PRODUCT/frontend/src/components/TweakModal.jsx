import { useEffect, useRef, useState } from 'react'

/**
 * Edit form modal used by both the "Adjust the model" (ER) and "Tweak the
 * mapping" (STTM) DDI gates. Lets the user enter free-text feedback that the
 * backend forwards to the relevant agent as user_feedback for refinement.
 */
export default function TweakModal({ mode = 'er', onSubmit, onClose }) {
  const [text, setText] = useState('')
  const taRef = useRef(null)

  useEffect(() => {
    if (taRef.current) taRef.current.focus()
  }, [])

  const titles = mode === 'sttm'
    ? {
        heading: 'Tweak the mapping',
        hint:    'Describe what you want to change in the Silver → Gold STTM.',
        placeholder: 'e.g. Rename ctr to click_through_rate; add a SUM measure for revenue.',
      }
    : {
        heading: 'Adjust the model',
        hint:    'Describe what you want to change about the gold-layer ER.',
        placeholder: 'e.g. Split dim_campaign into dim_campaign + dim_audience; drop year on dim_date.',
      }

  const handleSubmit = (e) => {
    e.preventDefault()
    if (!text.trim()) return
    onSubmit(text.trim())
  }

  return (
    <div className="erf-overlay">
      <div className="erf-panel" style={{ maxHeight: '60vh' }}>
        <div className="erf-header">
          <div className="erf-title">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
              <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
              <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
            </svg>
            {titles.heading}
          </div>
          <button className="erf-close" onClick={onClose} type="button" aria-label="Close">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        <form className="erf-body" onSubmit={handleSubmit}>
          <div className="erf-grid">
            <div className="erf-field erf-field-full">
              <label className="erf-label">
                Change description
                <span className="erf-hint">{titles.hint}</span>
              </label>
              <textarea
                ref={taRef}
                className="erf-textarea"
                rows={6}
                value={text}
                onChange={e => setText(e.target.value)}
                placeholder={titles.placeholder}
              />
            </div>
          </div>

          <div className="erf-footer">
            <button type="button" className="erf-cancel" onClick={onClose}>Cancel</button>
            <button type="submit" className="erf-submit" disabled={!text.trim()}>
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                <polyline points="20 6 9 17 4 12" />
              </svg>
              Apply changes
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
