import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import DiscoveryResultCard from './DiscoveryResultCard'
import BusinessGlossaryCard from './BusinessGlossaryCard'
import ClassificationCard from './ClassificationCard'
import ChallengerCard from './ChallengerCard'
import StartingPointCards from './StartingPointCards'
import MermaidBlock from './MermaidBlock'
import STTMCard from './STTMCard'
import DataContractCard from './DataContractCard'

const COLLAPSE_THRESHOLD_PX = 320

function _hasLanguageTaggedCode(children) {
  const arr = Array.isArray(children) ? children : [children]
  for (const child of arr) {
    if (child && child.props && typeof child.props.className === 'string') {
      if (/language-\w+/.test(child.props.className)) return true
    }
  }
  return false
}

const MD_COMPONENTS = {
  code({ inline, className, children, ...props }) {
    const match = /language-(\w+)/.exec(className || '')
    const lang = match ? match[1] : ''
    const value = String(children).replace(/\n$/, '')
    if (!inline && lang === 'mermaid') {
      return <MermaidBlock chart={value} />
    }
    if (!inline && !lang) {
      return <>{value}</>
    }
    return <code className={className} {...props}>{children}</code>
  },
  pre({ children }) {
    if (_hasLanguageTaggedCode(children)) {
      return <pre className="md-code-block">{children}</pre>
    }
    return <>{children}</>
  },
}

function agentInitials(agentName) {
  if (!agentName) return 'BA'
  const words = agentName.trim().split(/\s+/)
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase()
  return agentName.slice(0, 2).toUpperCase()
}

const INFO_PATTERN_HANDOFF = /\bReady for handoff\b/i
const INTERNAL_HEAD_PATTERN = /^(?:\*\*)?(?:STEP\s*\d|Step\s*\d|Silent\s*extraction|Full[-\s]?thread\s*extraction|Pass\s*\d)/i
const INTERNAL_LINE_PATTERN = /^\s*(?:\*\*)?(?:STEP\s*\d|Silent\s*extraction|Full[-\s]?thread\s*extraction|Phase\s*[AB]\s*asked\??\s*[:—-]|Pass\s*\d)\b/i
const SECTION_BREAK = /^(?:-{3,}|[─━—=]{4,})$/

function cleanIntroText(text) {
  if (!text) return ''
  let t = String(text).trim()
  if (t.startsWith('```')) {
    t = t.replace(/^```[a-zA-Z]*\s*/, '').replace(/```$/, '').trim()
  }
  return t.split('\n').map(l => l.replace(/^[ \t]+/, '')).join('\n').trim()
}

function cleanAgentText(text) {
  if (!text) return text
  const lines = String(text).split('\n')
  const firstNonEmpty = lines.find(l => l.trim().length > 0) || ''
  if (INTERNAL_HEAD_PATTERN.test(firstNonEmpty.trim())) {
    let cut = -1
    for (let i = 1; i < lines.length; i++) {
      const ln = lines[i].trim()
      if (SECTION_BREAK.test(ln))      { cut = i + 1; break }
      if (/^## /.test(ln))             { cut = i;     break }
      if (ln.startsWith('{'))          { cut = i;     break }
    }
    if (cut > 0 && cut < lines.length) {
      return cleanAgentText(lines.slice(cut).join('\n').replace(/^\s+/, ''))
    }
  }

  const kept = lines.filter(l => !INTERNAL_LINE_PATTERN.test(l))
  const out = []
  let skipBlock = false
  for (const line of kept) {
    const t = line.trim()
    if (!t) { skipBlock = false; out.push(line); continue }
    if (skipBlock) continue
    if (/^\s*(?:>\s*)?(?:#{1,6}\s+)?(?:\*\*)?Available actions\b/i.test(line)) {
      skipBlock = true
      continue
    }
    out.push(line)
  }
  return out.join('\n').replace(/\n{3,}/g, '\n\n').trim()
}

function challengerToBulletMarkdown(text) {
  const cleaned = cleanIntroText(cleanAgentText(text))
  if (!cleaned) return ''
  const lines = cleaned.split('\n').map(l => l.trim()).filter(Boolean)
  let items
  if (lines.length > 1 || /^[-*•]\s+/.test(lines[0] || '')) {
    items = lines.map(l => l.replace(/^[-*•]\s+/, ''))
  } else {
    items = (lines[0] || '')
      .split(/(?<=[.!?])\s+(?=[A-Z*])/)
      .map(s => s.trim())
      .filter(Boolean)
  }
  return items.map(s => `- ${s}`).join('\n')
}

function ChallengerNarrative({ text }) {
  const md = challengerToBulletMarkdown(text)
  if (!md) return null
  return (
    <div className="ch-intro ch-narrative">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>{md}</ReactMarkdown>
    </div>
  )
}

function formatHandoffBlock(block) {
  const cleaned = block.replace(/^>\s?/gm, '').trim()
  const parts = cleaned.split(/\*\*([^*\n]+?):\*\*/g)
  if (parts.length < 3) return null
  const bullets = []
  for (let i = 1; i < parts.length; i += 2) {
    const label = parts[i].trim()
    const value = (parts[i + 1] || '').trim()
    if (!label) continue
    bullets.push(`> - **${label}:** ${value || '—'}`)
  }
  return bullets.length ? bullets.join('\n') : null
}

function splitBronzeNarrative(text) {
  if (!text) return text
  const blocks = text.split(/\n{2,}/)

  const sentenceSplit = (s) =>
    s.split(/(?<=[.;])\s+(?=[A-Z(])/).map(p => p.trim()).filter(Boolean)

  const out = blocks.map(block => {
    const trimmed = block.trim()
    if (!trimmed) return block
    if (/^([-*]|>|#|`{3})/.test(trimmed)) return block

    const numberedRe = /(?:\([0-9]+\)|(?:^|\s)[0-9]+\.\s)/g
    const matches = [...trimmed.matchAll(numberedRe)]
    if (matches.length >= 2 && trimmed.length >= 200) {
      const firstIdx = matches[0].index
      const intro = trimmed.slice(0, firstIdx).trim().replace(/[:;,]\s*$/, '')
      const listPart = trimmed.slice(firstIdx)
      const introBullets = intro ? sentenceSplit(intro) : []
      const items = listPart
        .split(/\([0-9]+\)|(?:^|\s)[0-9]+\.\s/)
        .map(s => s.trim().replace(/^[;,]\s*/, '').replace(/[;]\s*$/, ''))
        .filter(Boolean)
      const bullets = [...introBullets, ...items]
      if (bullets.length > 1) {
        return bullets.map(b => `- ${b}`).join('\n')
      }
    }

    if (trimmed.length >= 300) {
      const sentences = sentenceSplit(trimmed)
      if (sentences.length >= 3) {
        return sentences.map(s => `- ${s}`).join('\n')
      }
    }

    return block
  })
  return out.join('\n\n')
}

function _significantTokens(s) {
  if (!s) return new Set()
  const lower = String(s).toLowerCase()
  const toks = new Set()
  for (const m of lower.matchAll(/`([^`]+)`/g)) toks.add(m[1].trim())
  for (const m of lower.matchAll(/[a-z][a-z0-9]*(?:_[a-z0-9]+)+/g)) toks.add(m[0])
  for (const m of lower.matchAll(/\b[a-z][a-z0-9]{4,}\b/g)) toks.add(m[0])
  return toks
}

function dedupBulletsAgainstParagraph(text) {
  if (!text) return text
  const blocks = text.split(/\n{2,}/)
  const out = blocks.map((block, idx) => {
    const lines = block.split('\n')
    const bulletIdxs = lines
      .map((l, i) => (/^\s*[-*]\s+/.test(l) ? i : -1))
      .filter(i => i >= 0)
    if (bulletIdxs.length === 0) return block
    const paraLinesThisBlock = lines.slice(0, bulletIdxs[0]).join(' ').trim()
    const prevBlock = idx > 0 ? blocks[idx - 1] : ''
    const prevIsBullet = /^\s*[-*]/.test(prevBlock.trim())
    const paragraphContext = (
      paraLinesThisBlock || (prevIsBullet ? '' : prevBlock)
    ).trim()
    if (!paragraphContext) return block
    const paraTokens = _significantTokens(paragraphContext)
    if (paraTokens.size < 3) return block

    const keptLines = lines.filter((l, i) => {
      if (!bulletIdxs.includes(i)) return true
      const body = l.replace(/^\s*[-*]\s+/, '').trim()
      const bulletTokens = _significantTokens(body)
      if (bulletTokens.size === 0) return true
      let hit = 0
      for (const t of bulletTokens) if (paraTokens.has(t)) hit++
      const overlap = hit / bulletTokens.size
      return overlap < 0.60
    })
    return keptLines.join('\n')
  })
  return out.join('\n\n')
}

function wrapInfoSections(text) {
  if (!text) return text
  const blocks = text.split(/\n{2,}/)
  return blocks.map(block => {
    const lines = block.split('\n')
    const alreadyQuoted = lines.every(l => !l.trim() || l.trim().startsWith('>'))
    if (!INFO_PATTERN_HANDOFF.test(block)) {
      return alreadyQuoted ? block : block
    }
    const reformatted = formatHandoffBlock(block)
    if (reformatted) return reformatted
    if (alreadyQuoted) return block
    return lines.map(l => {
      const t = l.trim()
      return t ? `> ${t}` : ''
    }).join('\n')
  }).join('\n\n')
}

function agentAvatarClass(agentName) {
  if (!agentName) return 'agent'
  if (agentName.toLowerCase().includes('challenger')) return 'agent challenger'
  return 'agent'
}

export default function MessageRow({ msg, onChipClick, sessionId }) {
  const isUser = msg.role === 'user'
  const bubbleRef = useRef(null)
  const [tooLong, setTooLong] = useState(false)
  const [collapsed, setCollapsed] = useState(false)

  useEffect(() => {
    if (isUser || msg.loading) return
    const el = bubbleRef.current
    if (!el) return

    const measure = () => {
      const h = el.scrollHeight
      setTooLong(h > COLLAPSE_THRESHOLD_PX)
    }
    measure()

    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [isUser, msg.loading, msg.text, msg.discovery_view, msg.glossary, msg.classification_view, msg.challenger_view, msg.sttm_view, msg.bronze_transform_view])

  const showChevron      = tooLong && !isUser && !msg.loading
  const isGlossary       = !isUser && !!msg.glossary
  const isClassification = !isUser && !!msg.classification_view
  const isChallenger     = !isUser && !!msg.challenger_view
  const isSttm           = !isUser && !!msg.sttm_view
  const isBronzeXform    = !isUser && !!msg.bronze_transform_view

  const avatarInitials = isUser ? 'SM' : agentInitials(msg.agent)
  const avatarClass    = isUser ? 'user' : agentAvatarClass(msg.agent)
  const isChallAgent   = !isUser && (msg.agent || '').toLowerCase().includes('challenger')

  return (
    <div className={`msg-row ${msg.role}`}>
      <div className={`msg-av ${avatarClass}`}>{avatarInitials}</div>
      <div className={`msg-body ${msg.discovery_view ? 'msg-body-discovery' : ''}`}>
        {!isUser && (
          <div className={`msg-label${isChallAgent ? ' msg-label-challenger' : ''}`}>
            {msg.agent || 'Data Product Assistant'}
            <span className="msg-ts">{msg.time}</span>
          </div>
        )}
        <div className="bubble-wrap">
          <div
            ref={bubbleRef}
            className={
              `bubble ${msg.role}` +
              (msg.loading ? ' thinking' : '') +
              (msg.discovery_view ? ' bubble-discovery' : '') +
              (isGlossary ? ' bubble-glossary' : '') +
              (isClassification ? ' bubble-classification' : '') +
              (isChallenger ? ' bubble-challenger' : '') +
              (collapsed && tooLong ? ' bubble-collapsed' : '')
            }
          >
            {msg.loading ? (
              <><span /><span /><span /></>
            ) : isUser ? (
              msg.text
            ) : msg.discovery_view ? (
              <DiscoveryResultCard view={msg.discovery_view} />
            ) : isGlossary ? (
              <>
                {msg.text && (
                  <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
                    {wrapInfoSections(cleanAgentText(msg.text))}
                  </ReactMarkdown>
                )}
                <BusinessGlossaryCard glossary={msg.glossary} />
              </>
            ) : isClassification ? (
              <ClassificationCard view={msg.classification_view} />
            ) : isChallenger ? (
              <>
                {msg.text && <ChallengerNarrative text={msg.text} />}
                <ChallengerCard view={msg.challenger_view} />
              </>
            ) : isSttm ? (
              <>
                {msg.text && (
                  <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
                    {wrapInfoSections(msg.text)}
                  </ReactMarkdown>
                )}
                <STTMCard view={msg.sttm_view} variant="bronze" />
              </>
            ) : isBronzeXform ? (
              <>
                {msg.text && (
                  <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
                    {wrapInfoSections(dedupBulletsAgainstParagraph(splitBronzeNarrative(msg.text)))}
                  </ReactMarkdown>
                )}
                {msg.data_contract_view && <DataContractCard view={msg.data_contract_view} sessionId={sessionId} apiBase={import.meta.env.VITE_API_URL || ''} />}
                <STTMCard view={msg.bronze_transform_view} variant="bronze" />
              </>
            ) : msg.data_contract_view ? (
              <>
                {msg.text && (
                  <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
                    {wrapInfoSections(cleanAgentText(msg.text))}
                  </ReactMarkdown>
                )}
                <DataContractCard view={msg.data_contract_view} sessionId={sessionId} apiBase={import.meta.env.VITE_API_URL || ''} />
              </>
            ) : isChallAgent ? (
              <ChallengerNarrative text={msg.text} />
            ) : (
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
                {wrapInfoSections(cleanAgentText(msg.text))}
              </ReactMarkdown>
            )}
          </div>
          {showChevron && (
            <button
              type="button"
              className="msg-collapse-btn"
              onClick={() => setCollapsed(c => !c)}
              aria-label={collapsed ? 'Expand response' : 'Collapse response'}
              title={collapsed ? 'Expand response' : 'Collapse response'}
            >
              {collapsed ? (
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="6 9 12 15 18 9" />
                </svg>
              ) : (
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="18 15 12 9 6 15" />
                </svg>
              )}
            </button>
          )}
        </div>
        {!msg.loading && msg.chips && msg.chips.length > 0 && (
          <div className="chips">
            {msg.chips.map((label, i) => (
              <button
                key={i}
                className={`chip${(i === 0 && (isGlossary || isChallAgent)) ? ' chip-primary' : ''}`}
                onClick={() => onChipClick(label)}
              >
                {label}
              </button>
            ))}
          </div>
        )}
        {isUser && <div className="msg-ts" style={{ marginTop: '4px' }}>{msg.time}</div>}
      </div>
    </div>
  )
}
