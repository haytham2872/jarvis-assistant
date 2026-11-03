import React, { useRef, useEffect } from 'react'
import { useStore } from '../../store'
import useReducedMotion from '../../useReducedMotion'

const N_BARS = 14

export default function AmplitudeMeter() {
  const amplitude = useStore(s => s.amplitude)
  const state     = useStore(s => s.state)
  const reduced   = useReducedMotion()
  const barsRef   = useRef(new Float32Array(N_BARS).fill(0))
  const ampRef    = useRef(amplitude)
  const stateRef  = useRef(state)
  const rafRef    = useRef(null)
  const containerRef = useRef(null)
  const valRef    = useRef(null)

  useEffect(() => { ampRef.current   = amplitude }, [amplitude])
  useEffect(() => { stateRef.current = state },     [state])

  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const barEls = container.querySelectorAll('.bar')

    const tick = () => {
      const a    = ampRef.current
      const live = stateRef.current === 'listening' || stateRef.current === 'speaking'

      for (let i = 0; i < N_BARS; i++) {
        if (live) {
          const center  = 1 - Math.abs((i - (N_BARS - 1) / 2) / ((N_BARS - 1) / 2))
          const target  = reduced ? a : Math.random() * (a + 0.05)
          barsRef.current[i] = barsRef.current[i] * 0.72 + target * 0.28
        } else {
          barsRef.current[i] *= 0.88
        }
        const h = Math.max(2, barsRef.current[i] * 100 * (live ? (0.5 + (1 - Math.abs((i - (N_BARS-1)/2) / ((N_BARS-1)/2))) * 0.5) : 0.3))
        if (barEls[i]) barEls[i].style.height = Math.min(100, h) + '%'
      }

      container.classList.toggle('live', live)

      if (valRef.current) {
        const smoothed = barsRef.current.reduce((s, v) => s + v, 0) / N_BARS
        valRef.current.textContent = smoothed.toFixed(2)
      }

      rafRef.current = requestAnimationFrame(tick)
    }
    rafRef.current = requestAnimationFrame(tick)
    return () => { if (rafRef.current) cancelAnimationFrame(rafRef.current) }
  }, [reduced])

  return (
    <div className="rail-block">
      <div className="lab">
        AMPLITUDE
        <span className="val" ref={valRef}>0.00</span>
      </div>
      <div className="meter" ref={containerRef}>
        {Array.from({ length: N_BARS }).map((_, i) => (
          <div key={i} className="bar" style={{ height: '8%' }} />
        ))}
      </div>
    </div>
  )
}
