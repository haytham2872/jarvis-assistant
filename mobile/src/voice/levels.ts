// What the orb follows (docs/archive/MOBILE-APP-PLAN.md M4): the real input level while the owner records and
// the real output level while JARVIS's voice plays. Levels go through this bus, not React state,
// so a sample never re-renders the screen or the conversation list: the orb subscribes and
// moves a native-driven animated value.
import type { AudioSample } from 'expo-audio';

import type { LocalState } from '../chat/reducer';
import type { SpeakState } from './speechQueue';

export type Level = number;   // 0..1
type Listener = (level: Level) => void;

export type LevelBus = {
  push(level: Level): void;
  subscribe(fn: Listener): () => void;
};

/** A bus that keeps at most one push per `minMs` (the playback tap fires far more often). */
export function createLevelBus(minMs = 50, now: () => number = Date.now): LevelBus {
  const listeners = new Set<Listener>();
  let last = -Infinity;
  return {
    push(level) {
      const t = now();
      const v = Number.isFinite(level) ? Math.max(0, Math.min(1, level)) : 0;
      if (v > 0 && t - last < minMs) return;   // silence always gets through, so the orb settles
      last = t;
      for (const fn of listeners) fn(v);
    },
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}

/** Root-mean-square of PCM frames (-1..1). */
export function rms(frames: readonly number[]): number {
  if (!frames.length) return 0;
  let sum = 0;
  for (const f of frames) sum += f * f;
  return Math.sqrt(sum / frames.length);
}

/** A playback sample as a 0..1 level: speech RMS sits around 0.02-0.25, so it is stretched. */
export function playbackLevel(sample: AudioSample): Level {
  const ch = sample.channels?.[0]?.frames;
  if (!ch?.length) return 0;
  return Math.max(0, Math.min(1, rms(ch) * 4));
}

export type OrbMode = 'idle' | 'listening' | 'transcribing' | 'thinking' | 'speaking';

/** One state for the orb, by what matters most right now. */
export function orbMode(p: { recording: boolean; speech: SpeakState; states: LocalState[] }): OrbMode {
  if (p.recording) return 'listening';
  if (p.speech.phase === 'speaking') return 'speaking';
  if (p.states.includes('transcribing')) return 'transcribing';
  if (p.speech.phase === 'loading') return 'thinking';
  if (p.states.some((s) => s === 'running' || s === 'queued' || s === 'sending')) return 'thinking';
  return 'idle';
}

export const ORB_LABEL: Record<OrbMode, string> = {
  idle: '', listening: 'Listening', transcribing: 'Transcribing', thinking: 'Thinking', speaking: 'Speaking',
};
