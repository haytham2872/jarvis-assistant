// What each state looks like (slice 6), from the desktop's STATES and MOOD tables
// (frontend/src/components/NeuralSphere.jsx, frontend/src/theme.js), so the two read as one.
import type { OrbMode } from '../voice/levels';

export type RGB = readonly [number, number, number];
export type OrbLook = { colorA: RGB; colorB: RGB; amp: number; speed: number; glow: number; motes: number };

export const LOOK: Record<OrbMode, OrbLook> = {
  idle: { colorA: [0.05, 0.45, 0.72], colorB: [0.18, 0.71, 0.94], amp: 0.11, speed: 0.26, glow: 0.30, motes: 0.42 },
  listening: { colorA: [0.55, 0.40, 0.05], colorB: [1.00, 0.70, 0.28], amp: 0.24, speed: 0.60, glow: 0.34, motes: 0.66 },
  transcribing: { colorA: [0.55, 0.40, 0.05], colorB: [1.00, 0.70, 0.28], amp: 0.18, speed: 0.45, glow: 0.30, motes: 0.55 },
  thinking: { colorA: [0.30, 0.10, 0.55], colorB: [0.63, 0.42, 0.84], amp: 0.40, speed: 1.00, glow: 0.38, motes: 1.00 },
  speaking: { colorA: [0.55, 0.72, 0.82], colorB: [0.86, 0.95, 1.00], amp: 0.28, speed: 0.75, glow: 0.22, motes: 0.62 },
};

/** JARVIS's mood tones (backend/brain/mood.py), as on the desktop. */
export const MOOD_RGB: Record<string, RGB> = {
  contemplative: [0.353, 0.553, 0.839],
  curious: [0.184, 0.714, 0.941],
  melancholic: [0.478, 0.424, 0.835],
  sardonic: [0.490, 0.573, 0.651],
  engaged: [0.910, 0.604, 0.310],
  restless: [0.898, 0.376, 0.302],
};

export type Mood = { tone: string; intensity: number } | null | undefined;
export const MOOD_MAX = 0.5;     // a light tint: at most half way, at full intensity

function mix(a: RGB, b: RGB, k: number): RGB {
  return [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k];
}

/** The state's colours with JARVIS's mood mixed in by its intensity (the desktop's blendMood). */
export function tinted(look: OrbLook, mood: Mood): OrbLook {
  const m = mood ? MOOD_RGB[mood.tone] : undefined;
  if (!m) return look;
  const k = Math.max(0, Math.min(MOOD_MAX, (mood?.intensity ?? 0) * MOOD_MAX));
  return { ...look, colorA: mix(look.colorA, m, k), colorB: mix(look.colorB, m, k) };
}

/** Below this size the orb is a badge: a bigger body, the halo hugging it, and no motes (they
 *  would be a pixel each, read as noise). */
export const SMALL_PX = 80;

/** How much of its box the body fills, and how many motes may show, at a size in points. */
export function sizing(size: number): { radius: number; motes: number } {
  return size < SMALL_PX ? { radius: 0.74, motes: 0 } : { radius: 0.6, motes: 1 };
}
