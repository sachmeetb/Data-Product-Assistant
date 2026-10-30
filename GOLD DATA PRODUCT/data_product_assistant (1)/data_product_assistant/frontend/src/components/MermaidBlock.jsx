// Renders a mermaid diagram from a chart-source string. Used by MessageRow
// to turn ```mermaid``` code blocks in agent replies into actual diagrams.

import { useEffect, useId, useRef, useState, useCallback } from 'react'
import mermaid from 'mermaid'

mermaid.initialize({
  startOnLoad: false,
  theme: 'neutral',
  securityLevel: 'loose',
  er: {
    useMaxWidth: true,
    entityPadding: 16,
    fontSize: 12,
  },
  flowchart: {
    useMaxWidth: true,
    htmlLabels: true,
    curve: 'basis',
  },
})

export default function MermaidBlock({ chart }) {
  const containerRef = useRef(null)
  const modalContainerRef = useRef(null)
  const rawId = useId()
  const id = `mm-${rawId.replace(/[^a-zA-Z0-9]/g, '')}`
  const modalId = `mm-modal-${rawId.replace(/[^a-zA-Z0-9]/g, '')}`

  const [error, setError] = useState(null)
  const [zoom, setZoom] = useState(1)
  const [modalZoom, setModalZoom] = useState(1)
  const [showRaw, setShowRaw] = useState(false)
  const [autoFit, setAutoFit] = useState(true)
  const [isFullscreen, setIsFullscreen] = useState(false)
  const [renderedSvg, setRenderedSvg] = useState('')

  const isEr = typeof chart === 'string' && chart.trim().startsWith('erDiagram')

  const sanitizeAndRenderSvg = useCallback((svgString, targetContainer, fitMode, currentZoom) => {
    if (!targetContainer) return
    targetContainer.innerHTML = svgString
    const svgEl = targetContainer.querySelector('svg')
    if (svgEl) {
      if (!svgEl.getAttribute('viewBox')) {
        const rawW = svgEl.getAttribute('width') || '800'
        const rawH = svgEl.getAttribute('height') || '600'
        const numW = parseFloat(rawW.replace(/px|pt|%/g, '')) || 800
        const numH = parseFloat(rawH.replace(/px|pt|%/g, '')) || 600
        svgEl.setAttribute('viewBox', `0 0 ${numW} ${numH}`)
      }
      svgEl.setAttribute('preserveAspectRatio', 'xMidYMid meet')
      if (fitMode) {
        svgEl.style.width = '100%'
        svgEl.style.maxWidth = '100%'
        svgEl.style.height = 'auto'
      } else {
        svgEl.style.width = 'auto'
        svgEl.style.maxWidth = 'none'
        svgEl.style.height = 'auto'
      }
      svgEl.style.display = 'block'
      svgEl.style.margin = '0 auto'
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    if (!chart || showRaw) return

    mermaid
      .render(id, chart)
      .then(({ svg }) => {
        if (cancelled) return
        setRenderedSvg(svg)
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

  useEffect(() => {
    if (renderedSvg && containerRef.current) {
      sanitizeAndRenderSvg(renderedSvg, containerRef.current, autoFit, zoom)
    }
  }, [renderedSvg, autoFit, zoom, sanitizeAndRenderSvg])

  useEffect(() => {
    if (isFullscreen && renderedSvg && modalContainerRef.current) {
      sanitizeAndRenderSvg(renderedSvg, modalContainerRef.current, false, modalZoom)
    }
  }, [isFullscreen, renderedSvg, modalZoom, sanitizeAndRenderSvg])

  // ESC key to exit fullscreen
  useEffect(() => {
    const handleKeyDown = (e) => {
      if (e.key === 'Escape' && isFullscreen) {
        setIsFullscreen(false)
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [isFullscreen])

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
    <>
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
            flexWrap: 'wrap',
            gap: 6,
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontWeight: 700, color: '#4B5563' }}>
            <span>{isEr ? '📊 Entity-Relationship (Star Schema) Diagram' : '🔄 Architecture Lineage'}</span>
            <span style={{ fontSize: 10, background: '#EDE9FE', color: '#6B21A8', padding: '1px 6px', borderRadius: 4 }}>
              Interactive
            </span>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 5, flexWrap: 'wrap' }}>
            <button
              type="button"
              onClick={() => {
                setAutoFit(false)
                setZoom((z) => Math.max(0.4, Number((z - 0.15).toFixed(2))))
              }}
              style={{ background: '#FFFFFF', border: '1px solid #D1D5DB', borderRadius: 4, padding: '2px 6px', fontSize: 10.5, cursor: 'pointer' }}
              title="Zoom out"
            >
              －
            </button>
            <span style={{ fontSize: 10, color: '#6B7280', minWidth: 32, textAlign: 'center' }}>
              {autoFit ? 'Auto' : `${Math.round(zoom * 100)}%`}
            </span>
            <button
              type="button"
              onClick={() => {
                setAutoFit(false)
                setZoom((z) => Math.min(2.5, Number((z + 0.15).toFixed(2))))
              }}
              style={{ background: '#FFFFFF', border: '1px solid #D1D5DB', borderRadius: 4, padding: '2px 6px', fontSize: 10.5, cursor: 'pointer' }}
              title="Zoom in"
            >
              ＋
            </button>
            <button
              type="button"
              onClick={() => {
                setAutoFit(!autoFit)
                if (!autoFit) setZoom(1)
              }}
              style={{
                background: autoFit ? '#EDE9FE' : '#FFFFFF',
                border: autoFit ? '1px solid #7C3AED' : '1px solid #D1D5DB',
                color: autoFit ? '#6B21A8' : '#374151',
                borderRadius: 4,
                padding: '2px 7px',
                fontSize: 10,
                fontWeight: autoFit ? 700 : 500,
                cursor: 'pointer',
              }}
              title="Fit diagram width to card container"
            >
              Fit Width
            </button>
            <button
              type="button"
              onClick={() => {
                setAutoFit(false)
                setZoom(1)
              }}
              style={{ background: '#FFFFFF', border: '1px solid #D1D5DB', borderRadius: 4, padding: '2px 6px', fontSize: 10, cursor: 'pointer' }}
              title="Reset Zoom to 100%"
            >
              100%
            </button>
            <button
              type="button"
              onClick={() => setIsFullscreen(true)}
              style={{
                background: '#FFFFFF',
                border: '1px solid #7C3AED',
                color: '#7C3AED',
                borderRadius: 4,
                padding: '2px 8px',
                fontSize: 10.5,
                fontWeight: 600,
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'center',
                gap: 3,
              }}
              title="Open full diagram in fullscreen modal"
            >
              <span>⛶</span> Fullscreen
            </button>
            <button
              type="button"
              onClick={() => setShowRaw(!showRaw)}
              style={{
                background: showRaw ? '#EDE9FE' : '#FFFFFF',
                border: '1px solid #D1D5DB',
                borderRadius: 4,
                padding: '2px 8px',
                fontSize: 10.5,
                cursor: 'pointer',
                color: showRaw ? '#6B21A8' : '#374151',
                fontWeight: 600,
              }}
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
              maxHeight: 650,
              minHeight: 220,
              background: '#FAFAFA',
              display: 'block',
              position: 'relative',
            }}
          >
            <div
              ref={containerRef}
              style={{
                width: autoFit ? '100%' : 'fit-content',
                minWidth: autoFit ? '100%' : 'fit-content',
                margin: '0 auto',
                transform: autoFit ? 'none' : `scale(${zoom})`,
                transformOrigin: 'top left',
                transition: 'transform 0.15s ease',
              }}
            />
          </div>
        )}
      </div>

      {/* Fullscreen Overlay Modal */}
      {isFullscreen && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 99999,
            background: 'rgba(17, 24, 39, 0.75)',
            backdropFilter: 'blur(3px)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            padding: 24,
          }}
          onClick={() => setIsFullscreen(false)}
        >
          <div
            style={{
              width: '95vw',
              height: '92vh',
              background: '#FFFFFF',
              borderRadius: 12,
              boxShadow: '0 25px 50px -12px rgba(0, 0, 0, 0.25)',
              display: 'flex',
              flexDirection: 'column',
              overflow: 'hidden',
            }}
            onClick={(e) => e.stopPropagation()}
          >
            {/* Modal Header */}
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                padding: '12px 20px',
                background: '#F9FAFB',
                borderBottom: '1px solid #E5E7EB',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ fontSize: 14, fontWeight: 700, color: '#111827' }}>
                  {isEr ? '📊 Entity-Relationship Diagram (Full View)' : '🔄 Architecture Lineage (Full View)'}
                </span>
                <span style={{ fontSize: 11, background: '#EDE9FE', color: '#6B21A8', padding: '2px 8px', borderRadius: 999, fontWeight: 600 }}>
                  High Resolution
                </span>
              </div>

              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <button
                  type="button"
                  onClick={() => setModalZoom((z) => Math.max(0.3, Number((z - 0.2).toFixed(2))))}
                  style={{ background: '#FFFFFF', border: '1px solid #D1D5DB', borderRadius: 4, padding: '4px 10px', fontSize: 12, cursor: 'pointer' }}
                >
                  － Zoom Out
                </button>
                <span style={{ fontSize: 12, fontWeight: 600, color: '#374151', minWidth: 44, textAlign: 'center' }}>
                  {Math.round(modalZoom * 100)}%
                </span>
                <button
                  type="button"
                  onClick={() => setModalZoom((z) => Math.min(3.0, Number((z + 0.2).toFixed(2))))}
                  style={{ background: '#FFFFFF', border: '1px solid #D1D5DB', borderRadius: 4, padding: '4px 10px', fontSize: 12, cursor: 'pointer' }}
                >
                  ＋ Zoom In
                </button>
                <button
                  type="button"
                  onClick={() => setModalZoom(1)}
                  style={{ background: '#FFFFFF', border: '1px solid #D1D5DB', borderRadius: 4, padding: '4px 10px', fontSize: 12, cursor: 'pointer' }}
                >
                  Reset (100%)
                </button>
                <button
                  type="button"
                  onClick={() => setIsFullscreen(false)}
                  style={{
                    background: '#EF4444',
                    border: 'none',
                    borderRadius: 6,
                    padding: '5px 12px',
                    fontSize: 12,
                    fontWeight: 700,
                    color: '#FFFFFF',
                    cursor: 'pointer',
                    marginLeft: 12,
                  }}
                >
                  ✕ Close
                </button>
              </div>
            </div>

            {/* Modal Body */}
            <div
              style={{
                flex: 1,
                overflow: 'auto',
                padding: 24,
                background: '#F9FAFB',
                position: 'relative',
              }}
            >
              <div
                ref={modalContainerRef}
                style={{
                  width: 'fit-content',
                  margin: '0 auto',
                  transform: `scale(${modalZoom})`,
                  transformOrigin: 'top center',
                  transition: 'transform 0.15s ease',
                }}
              />
            </div>
          </div>
        </div>
      )}
    </>
  )
}
