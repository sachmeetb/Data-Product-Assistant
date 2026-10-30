// Renders a mermaid diagram from a chart-source string. Used by MessageRow
// to turn ```mermaid``` code blocks in agent replies into actual diagrams.

import { useEffect, useId, useRef, useState } from 'react'
import mermaid from 'mermaid'

mermaid.initialize({
  startOnLoad: false,
  theme: 'neutral',
  securityLevel: 'loose',
  er: {
    useMaxWidth: false,
    entityPadding: 16,
    fontSize: 12,
  },
  flowchart: {
    useMaxWidth: true,
  },
})

export default function MermaidBlock({ chart }) {
  const containerRef = useRef(null)
  const rawId = useId()
  const id = `mm-${rawId.replace(/[^a-zA-Z0-9]/g, '')}`
  const [error, setError] = useState(null)
  const [zoom, setZoom] = useState(1)
  const [showRaw, setShowRaw] = useState(false)

  const isEr = typeof chart === 'string' && chart.trim().startsWith('erDiagram')

  useEffect(() => {
    let cancelled = false
    if (!chart || !containerRef.current || showRaw) return

    mermaid
      .render(id, chart)
      .then(({ svg }) => {
        if (cancelled || !containerRef.current) return
        containerRef.current.innerHTML = svg
        setError(null)
      })
      .catch((err) => {
        if (cancelled) return
        setError(err?.message || String(err))
      })

    return () => {
      cancelled = true
    }
  }, [chart, id, showRaw])

  if (error) {
    return (
      <div style={{ background: '#FFF5F5', border: '1px solid #FEB2B2', borderRadius: 8, padding: 12, margin: '10px 0' }}>
        <div style={{ fontSize: 11.5, fontWeight: 700, color: '#C53030', marginBottom: 6 }}>
          Could not render diagram visually — showing raw specification:
        </div>
        <pre style={{ fontSize: 11, fontFamily: 'monospace', background: '#FFFFFF', padding: 8, borderRadius: 4, overflowX: 'auto' }}>
          {chart}
        </pre>
      </div>
    )
  }

  return (
    <div
      style={{
        background: '#FFFFFF',
        border: '1px solid #E5E7EB',
        borderRadius: 8,
        margin: '12px 0',
        overflow: 'hidden',
        boxShadow: '0 1px 3px rgba(0,0,0,0.05)',
      }}
    >
      {/* Diagram Top Bar */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          background: '#F9FAFB',
          borderBottom: '1px solid #E5E7EB',
          padding: '6px 12px',
          fontSize: 11,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontWeight: 700, color: '#4B5563' }}>
          <span>{isEr ? '📊 Entity-Relationship (Star Schema) Diagram' : '🔄 Architecture Lineage'}</span>
          <span style={{ fontSize: 10, background: '#EDE9FE', color: '#6B21A8', padding: '1px 6px', borderRadius: 4 }}>
            Interactive
          </span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <button
            type="button"
            onClick={() => setZoom((z) => Math.max(0.6, z - 0.15))}
            style={{ background: '#FFFFFF', border: '1px solid #D1D5DB', borderRadius: 4, padding: '2px 6px', fontSize: 10.5, cursor: 'pointer' }}
            title="Zoom out"
          >
            －
          </button>
          <span style={{ fontSize: 10, color: '#6B7280', minWidth: 32, textAlign: 'center' }}>
            {Math.round(zoom * 100)}%
          </span>
          <button
            type="button"
            onClick={() => setZoom((z) => Math.min(1.8, z + 0.15))}
            style={{ background: '#FFFFFF', border: '1px solid #D1D5DB', borderRadius: 4, padding: '2px 6px', fontSize: 10.5, cursor: 'pointer' }}
            title="Zoom in"
          >
            ＋
          </button>
          <button
            type="button"
            onClick={() => setZoom(1)}
            style={{ background: '#FFFFFF', border: '1px solid #D1D5DB', borderRadius: 4, padding: '2px 6px', fontSize: 10, cursor: 'pointer' }}
            title="Reset Zoom"
          >
            Reset
          </button>
          <button
            type="button"
            onClick={() => setShowRaw(!showRaw)}
            style={{ background: showRaw ? '#EDE9FE' : '#FFFFFF', border: '1px solid #D1D5DB', borderRadius: 4, padding: '2px 8px', fontSize: 10.5, cursor: 'pointer', color: showRaw ? '#6B21A8' : '#374151', fontWeight: 600 }}
          >
            {showRaw ? 'Show Diagram' : 'View Code'}
          </button>
        </div>
      </div>

      {/* Viewport */}
      {showRaw ? (
        <pre style={{ fontSize: 11, fontFamily: 'monospace', padding: 12, margin: 0, background: '#F9FAFB', overflowX: 'auto', color: '#1F2937' }}>
          {chart}
        </pre>
      ) : (
        <div
          style={{
            padding: 16,
            overflow: 'auto',
            maxHeight: 520,
            display: 'flex',
            justifyContent: 'center',
            alignItems: 'center',
            background: '#FAFAFA',
          }}
        >
          <div
            ref={containerRef}
            style={{
              transform: `scale(${zoom})`,
              transformOrigin: 'top center',
              transition: 'transform 0.15s ease',
              minWidth: 'fit-content',
            }}
          />
        </div>
      )}
    </div>
  )
}
