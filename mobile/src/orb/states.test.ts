import { LOOK, MOOD_MAX, MOOD_RGB, sizing, SMALL_PX, tinted } from './states';

it('every orb state has a look, and the desktop colours', () => {
  for (const m of ['idle', 'listening', 'transcribing', 'thinking', 'speaking'] as const) expect(LOOK[m]).toBeDefined();
  expect(LOOK.idle.colorB).toEqual([0.18, 0.71, 0.94]);
  expect(LOOK.thinking.amp).toBe(0.4);
});

it('his mood tints the colours, at most half way, by its intensity', () => {
  const full = tinted(LOOK.idle, { tone: 'restless', intensity: 1 });
  const r = MOOD_RGB.restless;
  full.colorA.forEach((v, i) => expect(v).toBeCloseTo(LOOK.idle.colorA[i] + (r[i] - LOOK.idle.colorA[i]) * MOOD_MAX));
  const light = tinted(LOOK.idle, { tone: 'restless', intensity: 0.2 });
  expect(light.colorB[0]).toBeCloseTo(LOOK.idle.colorB[0] + (r[0] - LOOK.idle.colorB[0]) * 0.1);
  expect(tinted(LOOK.idle, { tone: 'restless', intensity: 7 }).colorA).toEqual(full.colorA);    // clamped
  expect(full.amp).toBe(LOOK.idle.amp);                                                         // colour only
});

it('no mood, an unknown tone or none at all leaves the state as it is', () => {
  expect(tinted(LOOK.speaking, null)).toBe(LOOK.speaking);
  expect(tinted(LOOK.speaking, { tone: 'ecstatic', intensity: 1 })).toBe(LOOK.speaking);
  expect(tinted(LOOK.speaking, { tone: 'curious', intensity: 0 }).colorA).toEqual(LOOK.speaking.colorA);
  expect(tinted(LOOK.speaking, { tone: 'curious', intensity: -1 }).colorA).toEqual(LOOK.speaking.colorA);
});

it('a small orb is a badge: bigger body, no motes', () => {
  expect(sizing(40)).toEqual({ radius: 0.74, motes: 0 });
  expect(sizing(SMALL_PX - 1).motes).toBe(0);
  expect(sizing(SMALL_PX)).toEqual({ radius: 0.6, motes: 1 });
});
