import { LOOK } from './states';
import { mixPacked, pack, uniformsFor } from './uniforms';

const frame = { size: 300, time: 4, phase: 1.2, level: 0.5, radius: 0.6, reduced: false };

it('packs a look in the order the shader reads it', () => {
  expect(pack(LOOK.thinking)).toEqual([0.30, 0.10, 0.55, 0.63, 0.42, 0.84, 0.40, 1.00, 0.38, 1.00]);
  expect(pack(LOOK.thinking, 0)[9]).toBe(0);
});

it('a state change eases from one look to the next, clamped at both ends', () => {
  const a = pack(LOOK.idle);
  const b = pack(LOOK.speaking);
  expect(mixPacked(a, b, 0)).toEqual(a);
  expect(mixPacked(a, b, 1)).toEqual(b);
  expect(mixPacked(a, b, 0.5)[6]).toBeCloseTo((a[6] + b[6]) / 2);
  expect(mixPacked(a, b, -2)).toEqual(a);
  expect(mixPacked(a, b, 9)).toEqual(b);
});

it('the uniforms carry the look, the clock and the voice', () => {
  const u = uniformsFor(pack(LOOK.speaking), frame);
  expect(u).toMatchObject({ resolution: [300, 300], time: 4, phase: 1.2, amp: 0.28, level: 0.5, glow: 0.22, radius: 0.6 });
  expect(u.colorA).toEqual([0.55, 0.72, 0.82]);
  expect(u.colorB).toEqual([0.86, 0.95, 1.0]);
  expect(uniformsFor(pack(LOOK.speaking), { ...frame, level: 3 }).level).toBe(1);
});

it('with Reduce Motion the rim keeps still and the voice only brightens the halo', () => {
  const u = uniformsFor(pack(LOOK.speaking), { ...frame, reduced: true });
  expect(u.amp).toBe(0);
  expect(u.level).toBe(0);
  expect(u.glow).toBeCloseTo(0.22 + 0.5 * 0.55);
});
