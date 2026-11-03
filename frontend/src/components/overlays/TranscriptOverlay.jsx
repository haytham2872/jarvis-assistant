import React, { useEffect, useRef } from 'react'
import { useStore } from '../../store'
import { modelBadge } from '../../modelLabel'

function fmtTime(ts) {
  if (!ts) return ''
  return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export default function TranscriptOverlay({ onClose }) {
  const transcript     = useStore(s => s.transcript)
  const clearTranscript = useStore(s => s.clearTranscript)
  const bodyRef        = useRef(null)

  useEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight
  }, [transcript])

  return (
    <div className="dpanel transcript show">
      <div className="dpanel-head">
        <span className="lab">
          TRANSCRIPT
          <span style={{ color: 'var(--t-dim)' }}> · {transcript.length} TURNS</span>
        </span>
        <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
          <button
            onClick={clearTranscript}
            style={{ background:'none', border:'none', cursor:'pointer',
              fontFamily:'var(--font-mono)', fontSize:11, color:'var(--t-dim)',
              letterSpacing:'.06em', textTransform:'uppercase' }}
          >
            CLEAR
          </button>
          <button className="ov-close" onClick={onClose}>✕</button>
        </div>
      </div>
      <div className="ov-body" ref={bodyRef}>
        {transcript.length === 0 && (
          <div style={{ color:'var(--t-faint)', fontFamily:'var(--font-mono)', fontSize:13 }}>
            No messages yet.
          </div>
        )}
        {transcript.map(t => {
          const isUser = t.role === 'user'
          const badge  = !isUser ? modelBadge(t.model) : null
          return (
            <div key={t.id} className={'t-turn ' + (isUser ? 'user' : 'jarvis')}>
              <div className="who">
                {isUser ? 'YOU' : 'JARVIS'}
                {badge && (
                  <span className="badge" style={{ fontSize:10, padding:'1px 7px' }}>
                    {badge}
                  </span>
                )}
                <span style={{ marginLeft:'auto', color:'var(--t-faint)', fontSize:10 }}>
                  {fmtTime(t.ts)}
                </span>
              </div>
              <div className="body">{t.text}</div>
              {t.tools?.length > 0 && (
                <div className="tools">
                  {t.tools.map((tool, i) => (
                    <span key={`${tool}-${i}`} className="chip">
                      <span className="tg">▸</span>{tool}
                    </span>
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
