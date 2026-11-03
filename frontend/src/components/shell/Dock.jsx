import React, { useState, useRef, useCallback } from 'react'
import { useStore } from '../../store'

const SILENCE_THRESHOLD   = 6
const SILENCE_DURATION_MS = 1500
const MIN_SPEECH_MS       = 400

const log  = (...a) => console.log(`[MIC  ${new Date().toISOString().slice(11,23)}]`, ...a)
const warn = (...a) => console.warn(`[MIC  ${new Date().toISOString().slice(11,23)}]`, ...a)

export default function Dock({ onSend, onAudioReady, onInterrupt, onPlanToggle, onMemToggle, planOn, memOn }) {
  const [text, setText]  = useState('')
  const [recording, setRec] = useState(false)

  const transcript = useStore(s => s.transcript)
  const state      = useStore(s => s.state)
  const connected  = useStore(s => s.connected)
  const setState   = useStore(s => s.setState)

  const recorderRef    = useRef(null)
  const streamRef      = useRef(null)
  const chunksRef      = useRef([])
  const intervalRef    = useRef(null)
  const speechStartRef = useRef(null)
  const silenceMsRef   = useRef(0)

  const busy         = state !== 'idle'
  const canRecord    = connected && state === 'idle' && !recording
  const canInterrupt = connected && state === 'speaking' && !recording

  const stopRecording = useCallback((reason = 'manual') => {
    log(`stopRecording — reason: ${reason}`)
    clearInterval(intervalRef.current); intervalRef.current = null
    if (recorderRef.current?.state !== 'inactive') recorderRef.current?.stop()
    streamRef.current?.getTracks().forEach(t => t.stop())
    streamRef.current = null
    setRec(false)
  }, [])

  const startRecording = useCallback(async () => {
    if (!canRecord) return
    log('Requesting mic…')
    let stream
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false })
      log('✅ Mic granted')
    } catch (err) {
      warn('❌ getUserMedia failed:', err.name)
      return
    }

    streamRef.current = stream
    chunksRef.current = []
    silenceMsRef.current = 0
    speechStartRef.current = Date.now()

    let audioCtx
    try {
      audioCtx = new AudioContext()
      const analyser = audioCtx.createAnalyser(); analyser.fftSize = 512
      audioCtx.createMediaStreamSource(stream).connect(analyser)
      const buf = new Uint8Array(analyser.fftSize)
      intervalRef.current = setInterval(() => {
        analyser.getByteTimeDomainData(buf)
        const rms = Math.sqrt(buf.reduce((s, v) => s + (v - 128) ** 2, 0) / buf.length)
        if (rms < SILENCE_THRESHOLD) {
          silenceMsRef.current += 100
          if (silenceMsRef.current >= SILENCE_DURATION_MS) stopRecording('silence')
        } else {
          silenceMsRef.current = 0
        }
      }, 100)
    } catch (e) { warn('AudioContext failed:', e) }

    const mimeType = MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : 'audio/ogg'
    const recorder = new MediaRecorder(stream, { mimeType })
    recorderRef.current = recorder

    recorder.ondataavailable = e => { if (e.data.size > 0) chunksRef.current.push(e.data) }
    recorder.onstop = () => {
      if (audioCtx) audioCtx.close()
      const elapsed = Date.now() - speechStartRef.current
      const bytes   = chunksRef.current.reduce((s, c) => s + c.size, 0)
      log(`Recording done — ${elapsed}ms, ${Math.round(bytes / 1024)}KB`)
      if (elapsed < MIN_SPEECH_MS || bytes < 1500) { setState('idle'); return }
      onAudioReady?.(new Blob(chunksRef.current, { type: mimeType }))
    }
    recorder.onerror = e => warn('Recorder error:', e.error)
    recorder.start(100)
    setRec(true)
    setState('listening')
  }, [canRecord, onAudioReady, setState, stopRecording])

  const handleMicClick = () => {
    if (recording)    return stopRecording('button')
    if (canInterrupt) return onInterrupt?.()
    if (canRecord)    return startRecording()
  }

  const submit = () => {
    const t = text.trim()
    if (!t || busy || !connected) return
    onSend?.(t)
    setText('')
  }

  const saveTranscript = () => {
    const lines = transcript.map(t =>
      `${t.role === 'user' ? 'YOU' : 'JARVIS'}: ${t.text}`
    ).join('\n\n')
    const blob = new Blob([lines], { type: 'text/plain' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `jarvis-${new Date().toISOString().slice(0, 10)}.txt`
    a.click()
    URL.revokeObjectURL(a.href)
  }

  const micArmed = recording || state === 'listening'
  const micLabel = canInterrupt ? '■ STOP' : recording ? '■ REC' : '● MIC'

  return (
    <div className="dock">
      <div className="dock-input">
        <input
          value={text}
          onChange={e => setText(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit() } }}
          placeholder={busy ? 'JARVIS is busy…' : 'Speak, or type a command…'}
          disabled={busy || !connected}
          autoComplete="off"
        />
      </div>
      <button
        className={'iconbtn summon' + (planOn ? ' on' : '')}
        onClick={onPlanToggle}
        title="Execution plan"
      >
        ▤ PLAN
      </button>
      <button
        className={'iconbtn summon' + (memOn ? ' on' : '')}
        onClick={onMemToggle}
        title="Memory inspector (Alt+M)"
      >
        ◫ MEM
      </button>
      <button
        className={'iconbtn mic' + (micArmed ? ' armed' : '')}
        onClick={handleMicClick}
        title={canInterrupt ? 'Interrupt JARVIS' : recording ? 'Stop recording' : 'Push to talk'}
      >
        {micLabel}
      </button>
      {transcript.length > 0 && (
        <button className="iconbtn" onClick={saveTranscript} title="Save transcript">
          ↓ SAVE
        </button>
      )}
    </div>
  )
}
