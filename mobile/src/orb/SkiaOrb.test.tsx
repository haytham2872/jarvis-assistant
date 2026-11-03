// The GPU orb's logic with Skia and Reanimated faked: what it hands the shader, how a state
// change eases, the voice level, and the clock stopping with Reduce Motion. (The shader itself is
// compiled for real by shader.test.ts.)
import { act, render } from '@testing-library/react-native';
import { AppState, type AppStateStatus } from 'react-native';

let mockFocused = true;
jest.mock('expo-router', () => ({ ...jest.requireActual('expo-router'), useIsFocused: () => mockFocused }));

import { createLevelBus } from '../voice/levels';
import { LOOK, tinted } from './states';
import { pack } from './uniforms';

type SV<T> = { value: T };
const mockR = {
  frames: [] as { cb: (i: { timeSincePreviousFrame: number | null }) => void; active: boolean; created: boolean; setActive: jest.Mock }[],
  derived: [] as { fn: () => unknown }[],
  timings: [] as { to: number; duration?: number }[],
  /** Where a timing lands at once: its target, or part way (`partial`) to see mid-ease. */
  partial: null as number | null,
};
jest.mock('react-native-reanimated', () => {
  const R = require('react');
  return {
    useSharedValue: (v: unknown) => R.useRef({ value: v }).current,
    useDerivedValue: (fn: () => unknown) => {
      const box = R.useRef({ fn, get value() { return this.fn(); } }).current;
      box.fn = fn;
      if (!mockR.derived.includes(box)) mockR.derived.push(box);
      return box;
    },
    useFrameCallback: (cb: (i: unknown) => void, active: boolean) => {
      const f = R.useRef(null).current ?? null;
      const ref = R.useRef(f);
      if (!ref.current) {
        ref.current = { cb, active, created: active, setActive: jest.fn((a: boolean) => { ref.current.active = a; }) };
        mockR.frames.push(ref.current);
      }
      ref.current.cb = cb;
      return ref.current;
    },
    withTiming: (to: number, cfg?: { duration?: number }) => {
      mockR.timings.push({ to, duration: cfg?.duration });
      return mockR.partial ?? to;              // lands at once unless a test stops it part way
    },
    Easing: { inOut: (f: unknown) => f, out: (f: unknown) => f, quad: 'quad' },
  };
});
const mockShader: { uniforms: SV<Record<string, unknown>> | null } = { uniforms: null };
jest.mock('@shopify/react-native-skia', () => ({
  Skia: { RuntimeEffect: { Make: () => ({ compiled: true }) } },
  Canvas: ({ children }: { children: unknown }) => children,
  Fill: ({ children }: { children: unknown }) => children,
  Shader: (p: { uniforms: SV<Record<string, unknown>> }) => { mockShader.uniforms = p.uniforms; return null; },
}));

import { SkiaOrb } from './SkiaOrb';

const u = () => mockShader.uniforms!.value as Record<string, number | number[]>;

beforeEach(() => {
  jest.spyOn(AppState, 'addEventListener').mockReturnValue({ remove: jest.fn() });
  mockFocused = true;
  Object.defineProperty(AppState, 'currentState', { configurable: true, value: 'active' });
  mockR.frames = [];
  mockR.derived = [];
  mockR.timings = [];
  mockR.partial = null;
  mockShader.uniforms = null;
});

it('stops the clock off screen, in the background and on unmount, then resumes on return', async () => {
  let changed!: (state: AppStateStatus) => void;
  const remove = jest.fn();
  const sub = jest.spyOn(AppState, 'addEventListener').mockImplementation((_name, fn) => {
    changed = fn; return { remove };
  });
  const bus = createLevelBus();
  const { rerender, unmount } = await render(<SkiaOrb mode="idle" bus={bus} size={300} reduced={false} />);
  const frame = mockR.frames[0];
  expect(frame.active).toBe(true);
  await act(() => changed('background'));
  expect(frame.active).toBe(false);
  await act(() => changed('active'));
  expect(frame.active).toBe(true);
  mockFocused = false;
  await rerender(<SkiaOrb mode="thinking" bus={bus} size={300} reduced={false} />);
  expect(frame.active).toBe(false);
  mockFocused = true;
  await rerender(<SkiaOrb mode="idle" bus={bus} size={300} reduced={false} />);
  expect(frame.active).toBe(true);
  await unmount();
  expect(frame.active).toBe(false);
  expect(remove).toHaveBeenCalled();
  sub.mockRestore();
});

it('hands the shader the state, the mood tint, the size and the room for its halo', async () => {
  const bus = createLevelBus();
  await render(<SkiaOrb mode="thinking" bus={bus} size={320} mood={{ tone: 'engaged', intensity: 0.6 }} reduced={false} />);
  const look = tinted(LOOK.thinking, { tone: 'engaged', intensity: 0.6 });
  expect(u().resolution).toEqual([320, 320]);
  expect(u().colorA).toEqual([...look.colorA]);
  expect(u().colorB).toEqual([...look.colorB]);
  expect(u()).toMatchObject({ amp: 0.4, radius: 0.6, motes: 1 });
});

it('a small orb gets the badge radius and no motes', async () => {
  await render(<SkiaOrb mode="idle" bus={createLevelBus()} size={40} reduced={false} />);
  expect(u()).toMatchObject({ radius: 0.74, motes: 0 });
});

it('a new state eases in from where the orb was, then shows the new look', async () => {
  const bus = createLevelBus();
  const { rerender } = await render(<SkiaOrb mode="idle" bus={bus} size={300} reduced={false} />);
  expect(mockR.timings.filter((t) => t.to === 1)).toHaveLength(0);            // nothing to ease on first sight
  await rerender(<SkiaOrb mode="speaking" bus={bus} size={300} reduced={false} />);
  expect(mockR.timings).toContainEqual({ to: 1, duration: 520 });
  expect(u().colorB).toEqual(pack(LOOK.speaking).slice(3, 6));
  expect(u().amp).toBe(0.28);
});

it('the voice moves it only while someone is speaking', async () => {
  const bus = createLevelBus(0);
  const { rerender } = await render(<SkiaOrb mode="thinking" bus={bus} size={300} reduced={false} />);
  await act(() => bus.push(0.8));
  expect(u().level).toBe(0);
  await rerender(<SkiaOrb mode="speaking" bus={bus} size={300} reduced={false} />);
  await act(() => bus.push(0.8));
  expect(u().level).toBe(0.8);
  await rerender(<SkiaOrb mode="idle" bus={bus} size={300} reduced={false} />);
  expect(u().level).toBe(0);                                                  // settles when the voice stops
});

it('the clock runs every frame, the rim at the state speed, and a dropped frame never jumps it', async () => {
  await render(<SkiaOrb mode="idle" bus={createLevelBus()} size={300} reduced={false} />);
  const f = mockR.frames[0];
  const t0 = u().time as number;
  const p0 = u().phase as number;
  f.cb({ timeSincePreviousFrame: 16 });
  expect((u().time as number) - t0).toBeCloseTo(0.016);
  expect((u().phase as number) - p0).toBeCloseTo(0.016 * LOOK.idle.speed);      // idle's slow 0.26, not 1
  f.cb({ timeSincePreviousFrame: 4000 });                                     // back from the background
  expect((u().time as number) - t0).toBeCloseTo(0.116);
});

it('with Reduce Motion the clock is stopped and the voice brightens instead of moving', async () => {
  const bus = createLevelBus(0);
  const { rerender } = await render(<SkiaOrb mode="speaking" bus={bus} size={300} reduced />);
  expect(mockR.frames[0].active).toBe(false);
  expect(mockR.frames[0].created).toBe(false);                               // never ticks, not even once
  await act(() => bus.push(0.6));
  expect(u()).toMatchObject({ amp: 0, level: 0 });
  expect(u().glow).toBeCloseTo(0.22 + 0.6 * 0.55);
  await rerender(<SkiaOrb mode="speaking" bus={bus} size={300} reduced={false} />);
  expect(mockR.frames[0].setActive).toHaveBeenLastCalledWith(true);
});

it('a change in the middle of an ease starts from where the orb is, not where it began', async () => {
  const bus = createLevelBus();
  const { rerender } = await render(<SkiaOrb mode="idle" bus={bus} size={300} reduced={false} />);
  mockR.partial = 0.5;
  await rerender(<SkiaOrb mode="speaking" bus={bus} size={300} reduced={false} />);
  const half = (a: number, b: number) => a + (b - a) * 0.5;
  const mid = half(LOOK.idle.amp, LOOK.speaking.amp);
  expect(u().amp).toBeCloseTo(mid);
  await rerender(<SkiaOrb mode="thinking" bus={bus} size={300} reduced={false} />);
  expect(u().amp).toBeCloseTo(half(mid, LOOK.thinking.amp));                 // not half way from idle
});

it('with Reduce Motion a new state shows at once, without an ease', async () => {
  const bus = createLevelBus();
  const { rerender } = await render(<SkiaOrb mode="idle" bus={bus} size={300} reduced />);
  await rerender(<SkiaOrb mode="thinking" bus={bus} size={300} reduced />);
  expect(mockR.timings).toContainEqual({ to: 1, duration: 0 });
});
