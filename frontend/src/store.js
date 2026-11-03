import { create } from 'zustand'

// Monotonic message id — Date.now() collides when two messages land in the
// same millisecond (user + assistant transcript of a fast turn), producing
// duplicate React keys.
let _nextId = 1
const nextId = () => _nextId++

// ── Transcript persistence ────────────────────────────────────────────────────
// The backend remembers across sessions (vault, mem0); the UI should at least
// remember across restarts. Cap mirrors the in-memory cap.
const TRANSCRIPT_KEY = 'jarvis_transcript_v1'

function loadTranscript() {
  try {
    const raw = localStorage.getItem(TRANSCRIPT_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    // Re-key with fresh monotonic ids so persisted ids never collide with new ones
    return parsed.slice(-50).map(t => ({ ...t, id: nextId() }))
  } catch {
    return []
  }
}

function saveTranscript(transcript) {
  try {
    localStorage.setItem(TRANSCRIPT_KEY, JSON.stringify(
      transcript.map(({ role, text, ts, model, tools }) => ({ role, text, ts, model, tools }))
    ))
  } catch { /* storage full / unavailable — non-fatal */ }
}

// Boot timestamp — entries restored from localStorage carry an older ts, so the
// live ConvoColumn can show only this-session turns while the Transcript overlay
// still browses the full restored history.
const SESSION_START_TS = Date.now()

export const useStore = create((set) => ({
  state: 'idle',
  transcript: loadTranscript(),
  sessionStartTs: SESSION_START_TS,
  thoughts: [],
  amplitude: 0,
  connected: false,
  currentResponse: '',
  mood: null,
  error: null,      // last error message surfaced to the user (toast)
  wakeActive: null, // null=unknown, true=wake word running, false=button-only mode

  // Per-turn metadata (from turn_meta + tool_call messages); attached to the
  // assistant transcript entry when it lands, then cleared.
  currentModel: null,  // model id handling the in-flight turn
  pendingTools: [],    // [{ tool, input }] tool calls made during the in-flight turn

  // Memory inspector overlay (Alt+M) — legacy field, superseded by activeOverlay
  inspectorOpen: false,

  // Active overlay: 'transcript' | 'plan' | 'memory' | null (one at a time)
  activeOverlay: null,

  // Plan-and-Execute state
  execPlan: [],      // [{ id, label, tool, parallel_group, status, tools? }]
  planActive: false, // true while an execution plan is shown
  currentStep: 0,   // 1-indexed step number currently executing

  setState: (s) => set({ state: s }),
  setConnected: (v) => set({ connected: v }),
  setAmplitude: (v) => set({ amplitude: v }),
  setMood: (m) => set({ mood: m }),
  setError: (msg) => set({ error: msg }),
  clearError: () => set({ error: null }),
  setWakeActive: (v) => set({ wakeActive: v }),
  toggleInspector: () => set((s) => ({ inspectorOpen: !s.inspectorOpen, activeOverlay: s.inspectorOpen ? null : 'memory' })),
  closeInspector: () => set({ inspectorOpen: false, activeOverlay: null }),

  openOverlay:  (name) => set((s) => ({ activeOverlay: s.activeOverlay === name ? null : name })),
  closeOverlay: ()     => set({ activeOverlay: null }),

  setTurnMeta: (model) => set({ currentModel: model, pendingTools: [] }),

  addToolCall: (tool, input) =>
    set((s) => ({
      pendingTools: [...s.pendingTools, { tool, input }].slice(-12),
    })),

  addTranscript: (role, text) =>
    set((s) => {
      const entry = { role, text, id: nextId(), ts: Date.now() }
      if (role === 'assistant') {
        entry.model = s.currentModel
        if (s.pendingTools.length) entry.tools = s.pendingTools.map(t => t.tool)
      }
      const transcript = [...s.transcript, entry].slice(-50)
      saveTranscript(transcript)
      return {
        transcript,
        currentResponse: role === 'assistant' ? '' : s.currentResponse,
        ...(role === 'assistant' ? { pendingTools: [] } : {}),
      }
    }),

  clearTranscript: () => {
    saveTranscript([])
    set({ transcript: [] })
  },

  appendResponse: (delta) =>
    set((s) => ({ currentResponse: s.currentResponse + delta })),

  // thoughts resets every turn (clearThoughts, on entering "thinking"), so a
  // per-turn array needs no cap here -- a delta-count slice silently dropped
  // the earliest chunks of any thinking stream over 200 events, cutting off
  // the start of the reasoning before it could ever render.
  addThought: (delta) =>
    set((s) => ({
      thoughts: [...s.thoughts, { delta, id: nextId() }],
    })),

  clearThoughts: () => set({ thoughts: [], currentResponse: '' }),

  // Initialize a new plan (sent once before execution begins)
  setPlan: (steps) => set({
    execPlan: steps.map(s => ({ ...s, status: 'pending' })),
    planActive: true,
    currentStep: 0,
    activeOverlay: 'plan',   // auto-summon plan overlay
  }),

  // Mark the next pending step as running
  stepStart: (n) => set((s) => {
    let marked = false
    return {
      currentStep: n,
      execPlan: s.execPlan.map(step => {
        if (!marked && step.status === 'pending') {
          marked = true
          return { ...step, status: 'running' }
        }
        return step
      }),
    }
  }),

  // Mark all running steps as done or failed
  stepDone: (success, tools, reason) => set((s) => ({
    execPlan: s.execPlan.map(step =>
      step.status === 'running'
        ? { ...step, status: success ? 'done' : 'failed', tools, reason: success ? undefined : reason }
        : step
    ),
  })),

  // Replace remaining pending steps after a replanner call
  updatePlan: (newSteps) => set((s) => ({
    execPlan: [
      ...s.execPlan.filter(st => st.status === 'done' || st.status === 'failed'),
      ...newSteps.map(st => ({ ...st, status: 'pending' })),
    ],
  })),

  // Reset plan (called when a new thinking turn starts)
  clearPlan: () => set({ execPlan: [], planActive: false, currentStep: 0 }),
}))
