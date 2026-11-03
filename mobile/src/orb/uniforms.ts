// The shader's uniforms from a state's look (slice 6). Pure and marked 'worklet', so the same
// function runs on the UI thread every frame and in the tests.
import type { OrbLook } from './states';

/** A look as ten numbers: colorA (3), colorB (3), amp, speed, glow, motes. */
export type Packed = number[];

export function pack(look: OrbLook, motesScale = 1): Packed {
  return [...look.colorA, ...look.colorB, look.amp, look.speed, look.glow, look.motes * motesScale];
}

/** Part way from one packed look to another (0..1), for a state change that eases in. */
export function mixPacked(a: Packed, b: Packed, k: number): Packed {
  'worklet';
  const out: number[] = [];
  for (let i = 0; i < b.length; i++) {
    const s = a[i] ?? b[i];
    out.push(k >= 1 ? b[i] : k <= 0 ? s : s + (b[i] - s) * k);    // exact at both ends
  }
  return out;
}

export type Frame = { size: number; time: number; phase: number; level: number; radius: number; reduced: boolean };

/** The uniforms for one frame. With Reduce Motion on, the rim stays still and the voice only
 *  brightens the halo; nothing grows. */
export function uniformsFor(p: Packed, f: Frame) {
  'worklet';
  const level = f.level < 0 ? 0 : f.level > 1 ? 1 : f.level;
  return {
    resolution: [f.size, f.size],
    time: f.time,
    phase: f.phase,
    amp: f.reduced ? 0 : p[6],
    colorA: [p[0], p[1], p[2]],
    colorB: [p[3], p[4], p[5]],
    level: f.reduced ? 0 : level,
    glow: p[8] + (f.reduced ? level * 0.55 : 0),
    motes: p[9],
    radius: f.radius,
  };
}
