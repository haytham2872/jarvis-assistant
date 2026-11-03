import React, { useEffect, useRef, useState } from 'react'
import { useStore } from '../../store'
import { modelBadge } from '../../modelLabel'

function ToolChips({ tools }) {
  if (!tools?.length) return null
  return (
    <>
      {tools.map((t, i) => (
        <span key={`${t}-${i}`} className="chip">
          <span className="tg">▸</span>{t}
        </span>
      ))}
    </>
  )
}

export default function ConvoColumn({ onOpenTranscript }) {
  const state           = useStore(s => s.state)
  const transcript      = useStore(s => s.transcript)
  const currentResponse = useStore(s => s.currentResponse)
  const thoughts        = useStore(s => s.thoughts)
  const currentModel    = useStore(s => s.currentModel)
  const pendingTools    = useStore(s => s.pendingTools)
  const sessionStartTs  = useStore(s => s.sessionStartTs)

  // Which stream is shown. Auto-follows the turn unless the user clicks a tab,
  // in which case the manual choice sticks until the next turn begins.
  const [tab, setTab]   = useState('response')
  const userPinnedRef   = useRef(false)
  const panelRef        = useRef(null)
  const atBottomRef     = useRef(true)

  const isStreaming = currentResponse.length > 0
  const isThinking  = state === 'thinking'
  const showCaret   = state === 'thinking' || state === 'speaking'
  const badge       = modelBadge(currentModel)
  const tools       = pendingTools.map(t => t.tool)

  // This-session entries only — restored history stays in the Transcript overlay.
  const live        = transcript.filter(t => t.ts >= sessionStartTs)
  const lastUser    = [...live].reverse().find(t => t.role === 'user')
  const lastAnswer  = [...live].reverse().find(t => t.role === 'assistant')

  // A new turn begins on entering 'thinking' — release any manual pin and follow.
  useEffect(() => {
    if (state === 'thinking') {
      userPinnedRef.current = false
      setTab('thinking')
    }
  }, [state])

  // Once the answer starts streaming, follow to response (unless user pinned).
  useEffect(() => {
    if (isStreaming && !userPinnedRef.current) setTab('response')
  }, [isStreaming])

  const onTab = (which) => { userPinnedRef.current = true; setTab(which) }

  // Track whether the user is scrolled to the bottom of the active panel.
  const onScroll = () => {
    const el = panelRef.current
    if (!el) return
    atBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24
  }

  // Auto-scroll only while the shown stream is actively growing and the user
  // hasn't scrolled up to read back.
  const growing = (tab === 'thinking' && isThinking) ||
                  (tab === 'response' && (isStreaming || state === 'speaking'))
  useEffect(() => {
    if (!growing || !atBottomRef.current) return
    const el = panelRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [thoughts, currentResponse, growing])

  // Reset scroll position to bottom when switching tabs.
  useEffect(() => {
    atBottomRef.current = true
    const el = panelRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [tab])

  const responseText = isStreaming ? currentResponse : (lastAnswer?.text ?? '')

  return (
    <div className="convo">
      <div className="convo-header">
        <span className="lab">CONVERSATION</span>
        <button className="expand lab" onClick={onOpenTranscript} title="Open full transcript">
          ⤢ TRANSCRIPT
        </button>
      </div>

      {/* recent user line — fixed on top of both streams */}
      {lastUser && (
        <div className="recent">
          <span className="who">YOU</span>
          {lastUser.text}
        </div>
      )}

      {/* turn meta: model badge + tool chips */}
      {(badge || tools.length > 0) && (
        <div className="turn-meta">
          {badge && <span className="badge">{badge}</span>}
          <ToolChips tools={tools} />
        </div>
      )}

      <div className="stream-area">
        <div className="stream-tabs">
          <button
            className={'stream-tab' + (tab === 'response' ? ' active' : '')}
            onClick={() => onTab('response')}
          >
            RESPONSE
          </button>
          <button
            className={'stream-tab' + (tab === 'thinking' ? ' active' : '')}
            onClick={() => onTab('thinking')}
          >
            THINKING
            {isThinking && <span className="tab-live" />}
          </button>
        </div>

        <div className="stream-panel" ref={panelRef} onScroll={onScroll}>
          {tab === 'thinking' ? (
            thoughts.length > 0 ? (
              <div className="reasoning">
                {thoughts.map(t => (
                  <span key={t.id} className="rl">{t.delta}</span>
                ))}
                {isThinking && <span className="caret" />}
              </div>
            ) : (
              <span className="empty-hint">
                {isThinking ? 'Thinking…' : 'No reasoning for this turn.'}
              </span>
            )
          ) : (
            <div className="response">
              {responseText ? (
                <>
                  {responseText}
                  {showCaret && isStreaming && <span className="caret" />}
                </>
              ) : (
                <span className="empty-hint">
                  {state === 'listening'
                    ? <span className="listening-line"><span className="ph">…</span></span>
                    : 'Awaiting input. Say the wake word or press the mic.'}
                </span>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
