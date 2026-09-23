// Renders a mermaid diagram from a chart-source string. Used by MessageRow
// to turn ```mermaid``` code blocks in agent replies into actual diagrams.

import { useEffect, useId, useRef, useState } from 'react'
import mermaid from 'mermaid'

mermaid.initialize({
  startOnLoad: false,
  theme: 'default',
  securityLevel: 'loose',
  er: { useMaxWidth: true },
  flowchart: { useMaxWidth: true },
})

export default function MermaidBlock({ chart }) {
  const containerRef = useRef(null)
  const rawId = useId()
  const id = `mm-${rawId.replace(/[^a-zA-Z0-9]/g, '')}`
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    if (!chart || !containerRef.current) return

    mermaid
      .render(id, chart)
      .then(({ svg }) => {
        if (cancelled || !containerRef.current) return
        containerRef.current.innerHTML = svg
        setError(null)
      })
      .catch(err => {
        if (cancelled) return
        setError(err?.message || String(err))
      })

    return () => { cancelled = true }
  }, [chart, id])

  if (error) {
    return (
      <div className="mermaid-block mermaid-block-error">
        <div className="mermaid-error-label">Could not render diagram — showing source:</div>
        <pre className="mermaid-source">{chart}</pre>
      </div>
    )
  }
  return <div className="mermaid-block" ref={containerRef} />
}
