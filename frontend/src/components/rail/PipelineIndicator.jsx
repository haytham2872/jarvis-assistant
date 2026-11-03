import React from 'react'
import { useStore } from '../../store'

const STATES = ['idle', 'listening', 'thinking', 'speaking']

export default function PipelineIndicator() {
  const state = useStore(s => s.state)

  return (
    <div className="rail-block">
      <div className="lab">PIPELINE</div>
      <div className="pipeline">
        {STATES.map(s => (
          <div
            key={s}
            className={'pipe-item' + (state === s ? ' active' : '')}
            data-s={s}
          >
            <span className="glyph" />
            {s}
          </div>
        ))}
      </div>
    </div>
  )
}
