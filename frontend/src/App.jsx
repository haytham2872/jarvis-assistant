import React, { useEffect, useRef } from 'react'
import { useStore } from './store'

// Shell components
import Stage          from './components/shell/Stage'
import TopStrip       from './components/shell/TopStrip'
import TelemetryRail  from './components/shell/TelemetryRail'
import ViewportFrame  from './components/shell/ViewportFrame'
import ConvoColumn    from './components/shell/ConvoColumn'
import Dock           from './components/shell/Dock'
import Veil           from './components/shell/Veil'
import ErrorBanner    from './components/shell/ErrorBanner'

// Content components
import NeuralSphere       from './components/NeuralSphere'

// Overlay components
import TranscriptOverlay  from './components/overlays/TranscriptOverlay'
import PlanOverlay        from './components/overlays/PlanOverlay'
import MemoryOverlay      from './components/overlays/MemoryOverlay'

const WS_URL = 'ws://localhost:8765/ws'
const RECONNECT_DELAY = 3000

const log  = (...a) => console.log(`[JARVIS ${new Date().toISOString().slice(11,23)}]`, ...a)
const warn = (...a) => console.warn(`[JARVIS ${new Date().toISOString().slice(11,23)}]`, ...a)

class ErrorBoundary extends React.Component {
  constructor(props) { super(props); this.state = { error: null } }
  static getDerivedStateFromError(e) { return { error: e } }
  componentDidCatch(e, i) { warn('UI crash:', e, i?.componentStack) }
  render() {
    if (this.state.error) {
      return (
        <div style={{
          position:'fixed', inset:0, display:'flex', flexDirection:'column',
          alignItems:'center', justifyContent:'center', gap:14,
          background:'var(--bg)', fontFamily:'var(--font-mono)',
          color:'var(--err)', textAlign:'center', padding:24,
        }}>
          <div style={{ fontSize:14 }}>The interface hit an unexpected error.</div>
          <div style={{ fontSize:11, opacity:.6, maxWidth:480 }}>
            {String(this.state.error?.message || this.state.error)}
          </div>
          <button
            onClick={() => this.setState({ error: null })}
            style={{
              padding:'6px 14px', fontFamily:'var(--font-mono)', fontSize:12,
              color:'var(--st-idle)', background:'transparent',
              border:'1px solid var(--st-idle)', borderRadius:'var(--r-sm)', cursor:'pointer',
            }}
          >
            Reload view
          </button>
        </div>
      )
    }
    return this.props.children
  }
}

export default function App() {
  return <ErrorBoundary><AppInner /></ErrorBoundary>
}

function AppInner() {
  const {
    setState, setConnected, addTranscript, addThought, clearThoughts,
    setAmplitude, appendResponse, setMood,
    setPlan, stepStart, stepDone, updatePlan, clearPlan,
    setError, setWakeActive, setTurnMeta, addToolCall,
    openOverlay, closeOverlay,
  } = useStore()

  const activeOverlay = useStore(s => s.activeOverlay)
  const wsRef           = useRef(null)
  const reconnectTimer  = useRef(null)
  const reconnectCount  = useRef(0)

  const sendMsg = (obj) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(obj))
    } else {
      warn('WS send DROPPED:', obj.type)
    }
  }

  const onTextSend = (text) => {
    log('Text send:', text)
    sendMsg({ type: 'text_input', text })
  }

  const onAudioReady = async (blob) => {
    log('Audio ready:', Math.round(blob.size / 1024), 'KB')
    try {
      const buf = await blob.arrayBuffer()
      const bytes = new Uint8Array(buf)
      // Encode in chunks — spreading the whole array into String.fromCharCode
      // overflows the call stack for anything but a tiny clip.
      let binary = ''
      const CHUNK = 0x8000
      for (let i = 0; i < bytes.length; i += CHUNK) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK))
      }
      const b64 = btoa(binary)
      sendMsg({ type: 'audio_data', data: b64 })
    } catch (err) {
      warn('Audio encode failed:', err)
    }
  }

  const onInterrupt = () => {
    log('Interrupt sent')
    sendMsg({ type: 'interrupt' })
  }

  const connect = () => {
    reconnectCount.current += 1
    log(`Connecting to ${WS_URL} (attempt #${reconnectCount.current})…`)
    const ws = new WebSocket(WS_URL)
    wsRef.current = ws

    ws.onopen = () => {
      log('✅ WebSocket connected')
      reconnectCount.current = 0
      setConnected(true)
      clearTimeout(reconnectTimer.current)
    }

    ws.onmessage = (e) => {
      let msg
      try { msg = JSON.parse(e.data) } catch { warn('Unparseable WS:', e.data); return }
      if (msg.type !== 'amplitude') {
        log('← WS:', msg.type, msg.value ?? msg.delta?.slice?.(0,40) ?? '')
      }
      switch (msg.type) {
        case 'state':
          setState(msg.value)
          if (msg.value === 'thinking') { clearThoughts(); clearPlan() }
          break
        case 'thinking':    addThought(msg.delta);                                                  break
        case 'response':    appendResponse(msg.delta);                                              break
        case 'transcript':  addTranscript(msg.role, msg.text);                                      break
        case 'amplitude':   setAmplitude(msg.value);                                                break
        case 'mood':        setMood({ tone: msg.tone, intensity: msg.intensity, trajectory: msg.trajectory }); break
        case 'turn_meta':   setTurnMeta(msg.model);                                                 break
        case 'tool_call':   addToolCall(msg.tool, msg.input);                                       break
        case 'plan':
          log('Plan:', msg.steps?.length, 'steps')
          setPlan(msg.steps ?? [])   // also sets activeOverlay:'plan' via store
          break
        case 'step_start':  stepStart(msg.n);                                                       break
        case 'step_done':   stepDone(msg.success !== false, msg.tools ?? [], msg.reason);           break
        case 'plan_update':
          log('Plan update:', msg.steps?.length, 'steps')
          updatePlan(msg.steps ?? [])
          break
        case 'wake_status': setWakeActive(msg.active !== false);                                    break
        case 'error':
          warn('Backend error:', msg.message)
          setError(msg.message || 'Something went wrong')
          break
        default: log('Unknown WS type:', msg.type)
      }
    }

    ws.onclose = (e) => {
      warn(`WS closed — code: ${e.code}`)
      setConnected(false)
      setState('idle')
      const delay = Math.min(RECONNECT_DELAY * reconnectCount.current, 15000)
      reconnectTimer.current = setTimeout(connect, delay)
    }

    ws.onerror = () => {
      warn('WS error')
      try { ws.close() } catch {}
    }
  }

  useEffect(() => {
    connect()
    const ping = setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: 'ping' }))
      }
    }, 30000)
    const onKey = (e) => {
      if (e.altKey && e.code === 'KeyM') { e.preventDefault(); openOverlay('memory') }
      if (e.key === 'Escape') closeOverlay()
    }
    window.addEventListener('keydown', onKey)
    return () => {
      clearInterval(ping)
      clearTimeout(reconnectTimer.current)
      window.removeEventListener('keydown', onKey)
      wsRef.current?.close()
    }
  }, [])

  return (
    <div style={{ width:'100vw', height:'100vh', background:'var(--bg)', overflow:'hidden' }}>
      <Stage>
        <div className="mood-wash" />
        <div className="bezel" />
        <div className="corner tl" /><div className="corner tr" />
        <div className="corner bl" /><div className="corner br" />

        <TopStrip />

        <div className="body-layout">
          <TelemetryRail />
          <ViewportFrame>
            <NeuralSphere />
          </ViewportFrame>
          <ConvoColumn onOpenTranscript={() => openOverlay('transcript')} />
        </div>

        <Dock
          onSend={onTextSend}
          onAudioReady={onAudioReady}
          onInterrupt={onInterrupt}
          onPlanToggle={() => openOverlay('plan')}
          onMemToggle={() => openOverlay('memory')}
          planOn={activeOverlay === 'plan'}
          memOn={activeOverlay === 'memory'}
        />

        <ErrorBanner />

        {/* Overlays + veil */}
        <Veil show={activeOverlay !== null} onClick={closeOverlay} />

        {activeOverlay === 'transcript' && (
          <TranscriptOverlay onClose={closeOverlay} />
        )}
        {activeOverlay === 'plan' && (
          <PlanOverlay onClose={closeOverlay} />
        )}
        {activeOverlay === 'memory' && (
          <MemoryOverlay onClose={closeOverlay} />
        )}
      </Stage>
    </div>
  )
}
