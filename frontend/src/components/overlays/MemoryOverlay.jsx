import React, { useEffect, useState } from 'react'

const INSPECTOR_URL = 'http://localhost:8765/inspector'

const TAB_LABELS = {
  queue:   'Intent',
  facts:   'Facts',
  letters: 'Letters',
  skills:  'Skills',
}
const TABS = ['queue', 'facts', 'letters', 'skills']

export default function MemoryOverlay({ onClose }) {
  const [data, setData] = useState(null)
  const [tab,  setTab]  = useState('queue')
  const [err,  setErr]  = useState(null)

  useEffect(() => {
    setErr(null)
    fetch(INSPECTOR_URL)
      .then(r => r.json())
      .then(setData)
      .catch(e => setErr(String(e)))
  }, [])

  function count(k) {
    if (!data) return ''
    if (k === 'queue') return ''
    const arr = data[k]
    return arr ? arr.length : ''
  }

  return (
    <div className="dpanel memory show">
      <div className="dpanel-head">
        <span className="lab">MEMORY INSPECTOR</span>
        <button className="ov-close" onClick={onClose}>✕</button>
      </div>

      <div className="mem-tabs">
        {TABS.map(t => (
          <button
            key={t}
            className={'mem-tab' + (tab === t ? ' active' : '')}
            onClick={() => setTab(t)}
          >
            {TAB_LABELS[t]}
            {count(t) !== '' && <span className="ct">{count(t)}</span>}
          </button>
        ))}
      </div>

      <div className="ov-body">
        {err && (
          <div style={{ color:'var(--err)', fontFamily:'var(--font-mono)', fontSize:13 }}>
            Backend unreachable: {err}
          </div>
        )}
        {!err && !data && (
          <div style={{ color:'var(--t-dim)', fontFamily:'var(--font-mono)', fontSize:13 }}>
            Loading…
          </div>
        )}

        {data && tab === 'queue' && (
          <pre style={{ fontFamily:'var(--font-mono)', fontSize:13, color:'var(--t-mid)',
            whiteSpace:'pre-wrap', wordBreak:'break-word', lineHeight:1.6 }}>
            {data.queue || '(empty)'}
          </pre>
        )}

        {data && tab === 'facts' && (
          <>
            <div style={{ fontFamily:'var(--font-mono)', fontSize:11, color:'var(--t-dim)',
              letterSpacing:'.08em', textTransform:'uppercase', marginBottom:10 }}>
              Unconsolidated facts ({data.facts?.length || 0})
            </div>
            {(data.facts?.length ? data.facts : []).map((f, i) => (
              <div key={i} className="mem-card" style={{ marginBottom:8 }}>
                <div className="mc-body">{f}</div>
              </div>
            ))}
            {!data.facts?.length && (
              <div style={{ color:'var(--t-faint)', fontFamily:'var(--font-mono)', fontSize:13 }}>
                (none — everything is consolidated)
              </div>
            )}
          </>
        )}

        {data && tab === 'letters' && (
          <>
            <div style={{ fontFamily:'var(--font-mono)', fontSize:11, color:'var(--t-dim)',
              letterSpacing:'.08em', textTransform:'uppercase', marginBottom:10 }}>
              Letters to self ({data.letters?.length || 0})
            </div>
            <div className="mem-grid">
              {(data.letters ?? []).map(l => (
                <div key={l.name} className="mem-card letter">
                  <div className="mc-head">
                    <span className="mc-title">{l.name}</span>
                    <span style={{ fontFamily:'var(--font-mono)', fontSize:10, color:'var(--t-dim)' }}>
                      {new Date(l.mtime * 1000).toLocaleDateString()}
                    </span>
                  </div>
                  <div className="mc-body">
                    {l.preview}{l.preview?.length >= 400 ? '…' : ''}
                  </div>
                </div>
              ))}
            </div>
            {!data.letters?.length && (
              <div style={{ color:'var(--t-faint)', fontFamily:'var(--font-mono)', fontSize:13 }}>
                (no letters yet)
              </div>
            )}
          </>
        )}

        {data && tab === 'skills' && (
          <>
            <div style={{ fontFamily:'var(--font-mono)', fontSize:11, color:'var(--t-dim)',
              letterSpacing:'.08em', textTransform:'uppercase', marginBottom:10 }}>
              Learned skills ({data.skills?.length || 0})
            </div>
            <div style={{ display:'flex', flexWrap:'wrap', gap:8 }}>
              {(data.skills ?? []).map(s => (
                <span key={s} className="status-pill" style={{ color:'var(--st-idle)' }}>
                  {s}
                </span>
              ))}
            </div>
            {!data.skills?.length && (
              <div style={{ color:'var(--t-faint)', fontFamily:'var(--font-mono)', fontSize:13 }}>
                (no skills learned yet)
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
