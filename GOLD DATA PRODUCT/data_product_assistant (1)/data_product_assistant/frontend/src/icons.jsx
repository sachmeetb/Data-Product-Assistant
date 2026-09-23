// icons.jsx — lucide icon bridge, ported from the prototype.
//
// The prototype loads lucide via UMD (window.lucide) and renders each icon by
// resolving its iconNode into an <svg>. We reproduce that exactly so the icon
// set + weights match pixel-for-pixel. Icons are looked up lazily on mount, so
// they work regardless of whether the CDN script finished before React booted.

import { useEffect, useRef } from 'react'

const ICON_KEY = {
  MessageSquare: 'message-square',
  Users: 'users',
  Calendar: 'calendar',
  FileText: 'file-text',
  Search: 'search',
  Paperclip: 'paperclip',
  Send: 'send',
  Video: 'video',
  Phone: 'phone',
  MoreHorizontal: 'more-horizontal',
  CheckCircle2: 'check-circle-2',
  Clock: 'clock',
  AlertTriangle: 'alert-triangle',
  Sparkles: 'sparkles',
  Download: 'download',
  FileSpreadsheet: 'file-spreadsheet',
  Bot: 'bot',
  Database: 'database',
  Layers: 'layers',
  Shield: 'shield',
  Wrench: 'wrench',
  Rocket: 'rocket',
  ChevronRight: 'chevron-right',
  ChevronDown: 'chevron-down',
  ChevronUp: 'chevron-up',
  Loader2: 'loader-circle',
  Zap: 'zap',
  BookOpen: 'book-open',
  CircleDot: 'circle-dot',
  ArrowRight: 'arrow-right',
  BarChart3: 'bar-chart-3',
  Bell: 'bell',
  X: 'x',
  Pencil: 'pencil',
  LogOut: 'log-out',
  RotateCcw: 'rotate-ccw',
}

function makeIcon(name) {
  return function IconCmp({ size = 16, color = 'currentColor', fill = 'none', className = '', style }) {
    const ref = useRef(null)
    useEffect(() => {
      if (!ref.current) return
      const lib = window.lucide
      if (!lib || !lib.icons) { ref.current.innerHTML = ''; return }
      const candidates = [name, ICON_KEY[name], name.replace(/([A-Z])/g, '-$1').toLowerCase().replace(/^-/, '')]
      if (name === 'Loader2') candidates.unshift('LoaderCircle', 'loader-circle')
      let node = null
      for (const k of candidates) {
        if (lib.icons[k]) { node = lib.icons[k]; break }
      }
      if (!node) { ref.current.innerHTML = ''; return }
      let svgString = ''
      try {
        let iconNode = null
        if (Array.isArray(node)) {
          iconNode = node[2] || []
        } else if (Array.isArray(node.iconNode)) {
          iconNode = node.iconNode
        } else if (typeof node.toSvg === 'function') {
          svgString = node.toSvg({ width: size, height: size, stroke: color, fill })
        }
        if (iconNode && !svgString) {
          const childSvg = iconNode.map(([ctag, cattrs]) => {
            const a = Object.entries(cattrs).map(([k, v]) => k + '="' + v + '"').join(' ')
            return '<' + ctag + ' ' + a + ' />'
          }).join('')
          svgString = '<svg xmlns="http://www.w3.org/2000/svg" width="' + size + '" height="' + size +
            '" viewBox="0 0 24 24" fill="' + fill + '" stroke="' + color +
            '" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + childSvg + '</svg>'
        }
      } catch (e) { /* icon lib shape mismatch — render nothing */ }
      ref.current.innerHTML = svgString || ''
    }, [size, color, fill])
    return (
      <span
        ref={ref}
        className={'lucide-icon ' + className}
        style={Object.assign({ display: 'inline-flex', alignItems: 'center', justifyContent: 'center', width: size, height: size }, style || {})}
      />
    )
  }
}

const ICONS = Object.fromEntries(Object.keys(ICON_KEY).map(n => [n, makeIcon(n)]))

export const {
  MessageSquare, Users, Calendar, FileText, Search, Paperclip, Send, Video, Phone,
  MoreHorizontal, CheckCircle2, Clock, AlertTriangle, Sparkles, Download, FileSpreadsheet,
  Bot, Database, Layers, Shield, Wrench, Rocket, ChevronRight, ChevronDown, ChevronUp,
  Loader2, Zap, BookOpen, CircleDot, ArrowRight, BarChart3, Bell, X, Pencil, LogOut, RotateCcw,
} = ICONS

export default ICONS
