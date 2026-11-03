import { useEffect, useState } from 'react'

// Tracks the OS "reduce motion" accessibility preference, live.
// Components use this to drop RAF-heavy / large-motion effects to a static
// presentation for users who get motion sickness or just prefer calm UI.
export default function useReducedMotion() {
  const [reduced, setReduced] = useState(
    () => typeof window !== 'undefined'
      && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  )

  useEffect(() => {
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    const onChange = (e) => setReduced(e.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  return reduced
}
