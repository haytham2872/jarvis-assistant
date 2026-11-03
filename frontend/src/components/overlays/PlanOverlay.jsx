import React from 'react'
import { useStore } from '../../store'

const ST_GLYPH = { done: '✓', running: '⟳', pending: '·', failed: '✕' }
const ST_CLASS = { done: 'done', running: 'running', pending: 'pending', failed: 'failed' }

export default function PlanOverlay({ onClose }) {
  const execPlan = useStore(s => s.execPlan)

  const done  = execPlan.filter(s => s.status === 'done').length
  const total = execPlan.length
  const pct   = total ? Math.round(done / total * 100) + '%' : '0%'

  return (
    <div className="dpanel plan show" style={{ left: '296px', width: '420px' }}>
      <div className="dpanel-head">
        <span className="lab">
          EXECUTION PLAN
          <span style={{ color: 'var(--t-dim)' }}> · {done}/{total}</span>
        </span>
        <button className="ov-close" onClick={onClose}>✕</button>
      </div>
      <div className="plan-progress">
        <div className="pf" style={{ width: pct }} />
      </div>
      <div className="ov-body" style={{ padding: '0 0 4px' }}>
        {execPlan.map((step, idx) => (
          <div key={`${step.id}-${idx}`} className={'plan-step ' + (ST_CLASS[step.status] ?? 'pending')}>
            <span className="num">{String(idx + 1).padStart(2, '0')}</span>
            <span className="g">{ST_GLYPH[step.status] ?? '·'}</span>
            <span className="name">
              {step.label}
              {step.parallel_group && step.status === 'pending' && (
                <span style={{ color:'var(--warn)', marginLeft:6, fontSize:10 }}>⚡</span>
              )}
              {step.status === 'failed' && step.reason && (
                <span style={{ display:'block', color:'var(--err)', fontSize:10, marginTop:2 }}>
                  {String(step.reason).slice(0, 120)}
                </span>
              )}
              {(step.tools?.length ? step.tools : step.tool ? [step.tool] : []).length > 0 && (
                <span style={{ display:'block', color:'var(--st-idle)', fontSize:10, marginTop:2 }}>
                  {(step.tools?.length ? step.tools : [step.tool]).join(', ')}
                </span>
              )}
            </span>
            <span className="st">{step.status}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
