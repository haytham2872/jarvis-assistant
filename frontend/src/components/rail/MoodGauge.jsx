import React, { useEffect, useRef } from 'react'
import { useStore } from '../../store'

const TRAJ_GLYPH = { rising: '↑ RISING', falling: '↓ FALLING', stable: '→ STABLE' }

export default function MoodGauge() {
  const mood     = useStore(s => s.mood)
  const stageRef = useRef(null)

  // Drive --mood on the Stage element so mood-wash + gauge share the same hue.
  // The Stage component also does this via store subscription, but gauge changes
  // can trigger here too for an immediate visual tie.
  useEffect(() => {
    if (!mood?.tone) return
    const stage = document.querySelector('.stage')
    if (stage) {
      stage.style.setProperty(
        '--mood',
        `var(--mood-${mood.tone}, var(--mood-engaged))`
      )
    }
  }, [mood?.tone])

  if (!mood) {
    return (
      <div className="rail-block" ref={stageRef}>
        <div className="lab">MOOD</div>
        <div className="mood-gauge">
          <div className="head">
            <span className="tone" style={{ color: 'var(--t-dim)' }}>—</span>
          </div>
          <div className="mood-track"><div className="mood-fill" style={{ width: '0%' }} /></div>
          <div className="sub">INTENSITY —</div>
        </div>
      </div>
    )
  }

  const { tone, intensity, trajectory } = mood
  const moodVar = `var(--mood-${tone}, var(--mood-engaged))`
  const pct = Math.round((intensity ?? 0) * 100) + '%'

  return (
    <div className="rail-block">
      <div className="lab">MOOD</div>
      <div className="mood-gauge">
        <div className="head">
          <span className="tone">{tone.toUpperCase()}</span>
          <span className="traj">{TRAJ_GLYPH[trajectory] ?? '→ STABLE'}</span>
        </div>
        <div className="mood-track">
          <div className="mood-fill" style={{ width: pct }} />
        </div>
        <div className="sub">INTENSITY {(intensity ?? 0).toFixed(2)}</div>
      </div>
    </div>
  )
}
