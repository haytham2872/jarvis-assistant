import React from 'react'
import { useStore } from '../../store'

const STATE_LABEL = {
  idle:      'IDLE',
  listening: 'LISTENING',
  thinking:  'THINKING',
  speaking:  'SPEAKING',
}

export default function ViewportFrame({ children }) {
  const state = useStore(s => s.state)

  return (
    <div className="viewport-frame">
      <span className="vp-label tl">NEURAL CORE</span>
      <span className="vp-label tr">{STATE_LABEL[state] ?? state.toUpperCase()}</span>
      <span className="vp-label bl">WEBGL</span>
      <div className="crosshair-v" />
      <div className="crosshair-h" />
      {children}
    </div>
  )
}
