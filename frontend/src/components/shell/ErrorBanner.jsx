import React, { useEffect } from 'react'
import { useStore } from '../../store'

export default function ErrorBanner() {
  const error      = useStore(s => s.error)
  const clearError = useStore(s => s.clearError)

  useEffect(() => {
    if (!error) return
    const t = setTimeout(clearError, 6000)
    return () => clearTimeout(t)
  }, [error, clearError])

  return (
    <div
      className={'error-banner' + (error ? ' show' : '')}
      onClick={clearError}
      role="alert"
      aria-live="assertive"
    >
      <span style={{ color: 'var(--err)' }}>⚠</span>
      <span>{error}</span>
      {error && <span style={{ marginLeft: 'auto', cursor: 'pointer', opacity: 0.5 }}>✕</span>}
    </div>
  )
}
