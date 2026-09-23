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
import { agentMeta } from '../theme'

const COLLAPSE_THRESHOLD_PX = 320

// Detect whether a <pre> wrapper contains a code block that was tagged with a
// language (```sql, ```python, ```json, etc.). Those must keep the <pre>
// wrapper so newlines + monospace + horizontal scroll work correctly.
// Untagged fenced blocks (``` … ```) from agent narratives should NOT use a
// <pre> wrapper — they render as plain prose.
function _hasLanguageTaggedCode(children) {
  // children is normally a single ReactElement <code className="language-sql"> ...
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
    // Untagged fenced blocks (``` … ```) from agent narratives should render
    // as normal prose, not monospace. Only honour a language hint.
    if (!inline && !lang) {
      return <>{value}</>
    }
    return <code className={className} {...props}>{children}</code>
  },
  pre({ children }) {
    // Keep the <pre> wrapper ONLY for language-tagged code blocks so the SQL /
    // JSON / Python preserves its newlines and indentation. For untagged
    // narrative fences (which we render as prose above), strip the <pre>.
    if (_hasLanguageTaggedCode(children)) {
      return <pre className="md-code-block">{children}</pre>
    }
    return <>{children}</>
  },
}

function agentInitials(agentName) {
  if (!agentName) return 'DA'
  const words = agentName.trim().split(/\s+/)
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase()
  return agentName.slice(0, 2).toUpperCase()
}

// "Ready for handoff" paragraphs are wrapped in a markdown blockquote so the
// CSS in index.css renders them as the green callout box.
const INFO_PATTERN_HANDOFF = /\bReady for handoff\b/i

// Leading internal-reasoning markers — when present at the top of a response
// the LLM has leaked its STEP 0 / Step 0 / Silent-extraction phase into the
// user-facing output. Defensive cleanup runs before render.
const INTERNAL_HEAD_PATTERN = /^(?:\*\*)?(?:STEP\s*\d|Step\s*\d|Silent\s*extraction|Full[-\s]?thread\s*extraction|Pass\s*\d)/i

// Lines that are pure internal commentary anywhere in the response.
const INTERNAL_LINE_PATTERN = /^\s*(?:\*\*)?(?:STEP\s*\d|Silent\s*extraction|Full[-\s]?thread\s*extraction|Phase\s*[AB]\s*asked\??\s*[:—-]|Pass\s*\d)\b/i

// Markdown horizontal rule, or the unicode box-drawing rule the SKILL uses,
// or a heading that signals real user-facing content begins.
const SECTION_BREAK = /^(?:-{3,}|[─━—=]{4,})$/

// The challenger intro is a single narrative paragraph from the LLM. Some
// model outputs come with leading whitespace or surrounding ``` fences, which
// ReactMarkdown treats as a code block — that forces monospace and disables
// wrapping. Render it as plain text after stripping those artefacts.
function cleanIntroText(text) {
  if (!text) return ''
  let t = String(text).trim()
  if (t.startsWith('```')) {
    t = t.replace(/^```[a-zA-Z]*\s*/, '').replace(/```$/, '').trim()
  }
  // Drop any leading whitespace on each line so markdown-style indent rules
  // don't matter even if a caller passes this back through markdown later.
  return t.split('\n').map(l => l.replace(/^[ \t]+/, '')).join('\n').trim()
}

// Strip any internal-reasoning leak that the LLM may emit despite the SKILL
// gag order, plus any "Available actions" callout (chips render as buttons
// already, so naming them in prose is redundant noise).
function cleanAgentText(text) {
  if (!text) return text
  const lines = String(text).split('\n')

  // ── (1) If the response opens with an internal-reasoning header, trim
  //        the entire leaked block off the front. Search forward for the
  //        first markdown HR, summary heading, or JSON object — that's
  //        where the real content begins.
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
      // Replace the leaked block with the clean tail
      return cleanAgentText(lines.slice(cut).join('\n').replace(/^\s+/, ''))
    }
    // No clean cut point — leave content alone rather than nuke a valid reply
  }

  // ── (2) Drop standalone internal-commentary lines anywhere in the body.
  const kept = lines.filter(l => !INTERNAL_LINE_PATTERN.test(l))

  // ── (3) Drop any "Available actions" paragraph entirely. The frontend
  //        already renders chip buttons; the LLM mentioning them is noise.
  const out = []
  let skipBlock = false
  for (const line of kept) {
    const t = line.trim()
    if (!t) { skipBlock = false; out.push(line); continue }
    if (skipBlock) continue
    // Recognise "Available actions:" with optional leading >, ## , bold, etc.
    if (/^\s*(?:>\s*)?(?:#{1,6}\s+)?(?:\*\*)?Available actions\b/i.test(line)) {
      skipBlock = true
      continue
    }
    out.push(line)
  }

  return out.join('\n').replace(/\n{3,}/g, '\n\n').trim()
}

// Turn the Challenger narrative into a markdown bullet list so it renders as
// discrete points rather than one wall-of-text paragraph. If the model already
// emitted bullet lines we just normalise them; if it sent a single paragraph
// (older prompt / un-redeployed backend) we split it on sentence boundaries —
// taking care not to break on decimals (e.g. "0.97") or mid-sentence bold.
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

// Convert a "Ready for handoff: ... Gaps: ... Marked unknown: ..." paragraph
// into a blockquoted bullet list so each item lands on its own line in the
// rendered card. Without this, the three labels collapse into a single
// run-on line because they're separated by newlines (not blank lines) in
// the source markdown — and ReactMarkdown joins those as a soft break.
function formatHandoffBlock(block) {
  const cleaned = block.replace(/^>\s?/gm, '').trim()
  // Split on **Label:** markers. parts[0] is preamble (usually empty);
  // then alternating label, value, label, value...
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

/**
 * For Silver Transformation Agent's text reply: detect long narrative
 * paragraphs that contain a numbered list inside them (e.g. "...issues: (1) X;
 * (2) Y; (3) Z." or "1. A 2. B 3. C") and rewrite each numbered item as a
 * markdown bullet so the paragraph renders as a structured list. Pre-amble
 * sentences (before the numbered markers) are also sentence-split into bullets.
 * Lines that are already bullets, headings, or short are left untouched.
 */
function splitSilverNarrative(text) {
  if (!text) return text
  const blocks = text.split(/\n{2,}/)

  const sentenceSplit = (s) =>
    s.split(/(?<=[.;])\s+(?=[A-Z(])/).map(p => p.trim()).filter(Boolean)

  const out = blocks.map(block => {
    const trimmed = block.trim()
    if (!trimmed) return block
    // Skip if already starts with a bullet, heading, fence, or blockquote
    if (/^([-*]|>|#|`{3})/.test(trimmed)) return block

    // Mode A: paragraph contains a numbered list like "(1) X (2) Y" — split at
    // each numbered marker. Pre-amble sentences are also bullet-split.
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

    // Mode B: long narrative paragraph WITHOUT numbered markers — sentence-split
    // it into bullets so it stays readable. Threshold higher (300+) so short
    // intro lines aren't bulleted needlessly.
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

/**
 * Extract "significant" tokens from a string — identifiers (words with _),
 * backticked code, and 5+ char words. These are the words a paragraph and a
 * bullet would share if they cover the same idea.
 */
function _significantTokens(s) {
  if (!s) return new Set()
  const lower = String(s).toLowerCase()
  const toks = new Set()
  // backticked code
  for (const m of lower.matchAll(/`([^`]+)`/g)) toks.add(m[1].trim())
  // snake_case identifiers
  for (const m of lower.matchAll(/[a-z][a-z0-9]*(?:_[a-z0-9]+)+/g)) toks.add(m[0])
  // long words (>= 5 chars), strip trailing punctuation
  for (const m of lower.matchAll(/\b[a-z][a-z0-9]{4,}\b/g)) toks.add(m[0])
  return toks
}

/**
 * Remove markdown bullet lines whose content significantly overlaps with the
 * preceding paragraph in the same block (>=60% of bullet's significant tokens
 * already appear in the paragraph). Preserves headings, fenced blocks, and
 * the paragraph itself.
 */
function dedupBulletsAgainstParagraph(text) {
  if (!text) return text
  const blocks = text.split(/\n{2,}/)
  const out = blocks.map((block, idx) => {
    // For each block, find a paragraph + bullet list combination
    const lines = block.split('\n')
    // Identify bullet lines vs paragraph lines
    const bulletIdxs = lines
      .map((l, i) => (/^\s*[-*]\s+/.test(l) ? i : -1))
      .filter(i => i >= 0)
    if (bulletIdxs.length === 0) return block
    // Treat everything BEFORE the first bullet as the paragraph; merge with
    // the previous block too in case the agent emitted "paragraph\n\nbullets"
    const paraLinesThisBlock = lines.slice(0, bulletIdxs[0]).join(' ').trim()
    const prevBlock = idx > 0 ? blocks[idx - 1] : ''
    const prevIsBullet = /^\s*[-*]/.test(prevBlock.trim())
    const paragraphContext = (
      paraLinesThisBlock || (prevIsBullet ? '' : prevBlock)
    ).trim()
    if (!paragraphContext) return block
    const paraTokens = _significantTokens(paragraphContext)
    if (paraTokens.size < 3) return block  // paragraph too short to dedup against

    const keptLines = lines.filter((l, i) => {
      if (!bulletIdxs.includes(i)) return true  // not a bullet, keep
      const body = l.replace(/^\s*[-*]\s+/, '').trim()
      const bulletTokens = _significantTokens(body)
      if (bulletTokens.size === 0) return true
      let hit = 0
      for (const t of bulletTokens) if (paraTokens.has(t)) hit++
      const overlap = hit / bulletTokens.size
      return overlap < 0.60  // strip if >= 60% of bullet tokens are in paragraph
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
    // Handoff status block — try the bullet reformat first.
    const reformatted = formatHandoffBlock(block)
    if (reformatted) return reformatted
    // Couldn't parse cleanly — fall back to the old line-by-line wrap so
    // the user at least sees a blockquote, not raw `**Field:**` runs.
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

export default function MessageRow({ msg, onChipClick }) {
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
  }, [isUser, msg.loading, msg.text, msg.discovery_view, msg.glossary, msg.classification_view, msg.challenger_view, msg.sttm_view, msg.silver_transform_view, msg.data_contract_view])

  const showChevron      = tooLong && !isUser && !msg.loading
  const isGlossary       = !isUser && !!msg.glossary
  const isClassification = !isUser && !!msg.classification_view
  const isChallenger     = !isUser && !!msg.challenger_view
  const isSttm           = !isUser && !!msg.sttm_view
  const isSilverXform    = !isUser && !!msg.silver_transform_view

  const avatarInitials = isUser ? 'SM' : agentInitials(msg.agent)
  const avatarClass    = isUser ? 'user' : agentAvatarClass(msg.agent)
  const isChallAgent   = !isUser && (msg.agent || '').toLowerCase().includes('challenger')

  const avatarStyle = isUser ? undefined : { background: agentMeta(msg.agent).color, borderColor: agentMeta(msg.agent).color }

  return (
    <div className={`msg-row ${msg.role}`}>
      <div className={`msg-av ${avatarClass}`} style={avatarStyle}>{avatarInitials}</div>
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
                <STTMCard view={msg.sttm_view} variant="gold" />
              </>
            ) : isSilverXform ? (
              <>
                {msg.text && (
                  <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
                    {wrapInfoSections(dedupBulletsAgainstParagraph(splitSilverNarrative(msg.text)))}
                  </ReactMarkdown>
                )}
                {msg.data_contract_view && <DataContractCard view={msg.data_contract_view} />}
                <STTMCard view={msg.silver_transform_view} variant="silver" />
              </>
            ) : isChallAgent ? (
              // Challenger Agent message that arrived without a structured view
              // (e.g. the second message in the split response, or upstream
              // backends that no longer attach challenger_view). Render the
              // narrative as a bullet list rather than a single paragraph.
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
                className={`chip${i === 0 ? ' chip-primary' : ''}`}
                onClick={() => onChipClick(label)}
              >
                {label}
              </button>
            ))}
          </div>
        )}
        {(msg.startingPoint || msg.startingPointDisabled) && (
          <StartingPointCards onPick={onChipClick} disabled={msg.startingPointDisabled} />
        )}
        {isUser && <div className="msg-ts" style={{ marginTop: '4px' }}>{msg.time}</div>}
      </div>
    </div>
  )
}
