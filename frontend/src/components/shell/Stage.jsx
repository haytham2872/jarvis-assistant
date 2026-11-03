import React, { useEffect, useRef } from 'react'
import { useStore } from '../../store'

const ACCENT_MAP = {
  idle:      'var(--st-idle)',
  listening: 'var(--st-listening)',
  thinking:  'var(--st-thinking)',
  speaking:  'var(--st-speaking)',
}

const MOOD_MAP = {
  contemplative: 'var(--mood-contemplative)',
  curious:       'var(--mood-curious)',
  melancholic:   'var(--mood-melancholic)',
  sardonic:      'var(--mood-sardonic)',
  engaged:       'var(--mood-engaged)',
  restless:      'var(--mood-restless)',
}

export default function Stage({ children }) {
  const state = useStore(s => s.state)
  const mood  = useStore(s => s.mood)
  const ref   = useRef(null)

  useEffect(() => {
    if (!ref.current) return
    ref.current.style.setProperty('--accent', ACCENT_MAP[state] ?? ACCENT_MAP.idle)
  }, [state])

  useEffect(() => {
    if (!ref.current || !mood?.tone) return
    ref.current.style.setProperty('--mood', MOOD_MAP[mood.tone] ?? MOOD_MAP.engaged)
  }, [mood])

  return (
    <div ref={ref} className="stage" data-state={state}>
      {children}
    </div>
  )
}
