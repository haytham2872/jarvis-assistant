import React, { useEffect, useState } from 'react'
import { useStore } from '../../store'
import { modelLabel } from '../../modelLabel'

function Clock() {
  const [time, setTime] = useState(() => new Date().toTimeString().slice(0, 8))
  useEffect(() => {
    const id = setInterval(() => setTime(new Date().toTimeString().slice(0, 8)), 1000)
    return () => clearInterval(id)
  }, [])
  return <span className="lab" style={{ color: 'var(--t-mid)' }}>{time}</span>
}

export default function TopStrip() {
  const connected  = useStore(s => s.connected)
  const wakeActive = useStore(s => s.wakeActive)
  const lastModel  = useStore(s => s.currentModel)

  const connColor = connected ? 'var(--ok)' : 'var(--err)'

  return (
    <div className="topstrip">
      <div className="left">
        <span
          className={'dot conn-dot' + (!connected ? ' reconnecting' : '')}
          style={{ background: connColor }}
        />
        <span className="lab mid">JARVIS // INSTRUMENT</span>
        {wakeActive === false && (
          <span className="lab" style={{ color: 'var(--warn)', fontSize: 11 }}>
            MIC ONLY
          </span>
        )}
      </div>
      <div className="right">
        <span
          className="lab"
          style={{ color: connected ? '' : 'var(--warn)' }}
        >
          {connected ? 'CONNECTED' : 'RECONNECTING'}
        </span>
        {lastModel && (
          <span className="lab mid">{modelLabel(lastModel)}</span>
        )}
        <Clock />
      </div>
    </div>
  )
}
