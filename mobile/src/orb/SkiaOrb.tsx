// The neural orb on the GPU (slice 6): the shader in ./shader.ts, drawn by Skia and moved by
// Reanimated on the UI thread. Nothing here re-renders per frame or per voice sample: the
// clock, the voice level and a state change's easing are shared values the shader reads.
// Loaded only when Skia, Reanimated and Worklets are all in the app (see NeuralOrb).
import { Canvas, Fill, Shader, Skia } from '@shopify/react-native-skia';
import { useIsFocused } from 'expo-router';
import { memo, useEffect, useRef, useState } from 'react';
import { AppState } from 'react-native';
import { Easing, useDerivedValue, useFrameCallback, useSharedValue, withTiming } from 'react-native-reanimated';

import type { LevelBus, OrbMode } from '../voice/levels';
import { ORB_SKSL } from './shader';
import { LOOK, sizing, tinted, type Mood } from './states';
import { mixPacked, pack, uniformsFor } from './uniforms';

/** Compiled once. Null when this device's Skia cannot compile it: NeuralOrb then shows the old orb. */
export const EFFECT = (() => {
  try {
    return Skia.RuntimeEffect.Make(ORB_SKSL);
  } catch {
    return null;
  }
})();

const EASE_MS = 520;              // a state change eases in rather than snapping
const STILL_TIME = 2.5;           // where the clock rests with Reduce Motion on
const MAX_DT = 0.1;               // a dropped frame never jumps the clock more than this

type Props = { mode: OrbMode; bus: LevelBus; size: number; mood?: Mood; reduced: boolean };

function SkiaOrbView({ mode, bus, size, mood, reduced }: Props) {
  const focused = useIsFocused();
  const [foreground, setForeground] = useState(AppState.currentState === 'active');
  useEffect(() => {
    const sub = AppState.addEventListener('change', (state) => setForeground(state === 'active'));
    return () => sub.remove();
  }, []);
  const z = sizing(size);
  const target = pack(tinted(LOOK[mode], mood), z.motes);
  const from = useSharedValue(target);
  const to = useSharedValue(target);
  const k = useSharedValue(1);
  const time = useSharedValue(STILL_TIME);
  const phase = useSharedValue(STILL_TIME * target[7]);
  const level = useSharedValue(0);
  const key = target.join(',');
  const first = useRef(true);

  // A new state (or mood, or size): ease from wherever the orb is now.
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    from.value = mixPacked(from.value, to.value, k.value);
    to.value = target;
    k.value = 0;
    k.value = withTiming(1, { duration: reduced ? 0 : EASE_MS, easing: Easing.inOut(Easing.quad) });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  // The real voice level, only while someone is speaking.
  const live = mode === 'listening' || mode === 'speaking';
  useEffect(() => {
    if (!live) {
      level.value = withTiming(0, { duration: 240 });
      return;
    }
    return bus.subscribe((v) => {
      level.value = withTiming(v, { duration: 90, easing: Easing.out(Easing.quad) });
    });
  }, [bus, live, level]);

  // The clock: still with Reduce Motion; the rim's phase runs at the state's own speed.
  const frame = useFrameCallback((info) => {
    'worklet';
    const dt = Math.min(MAX_DT, (info.timeSincePreviousFrame ?? 16) / 1000);
    const speed = to.value[7] * k.value + from.value[7] * (1 - k.value);
    time.value += dt;
    phase.value += dt * speed;
  }, !reduced && focused && foreground);
  useEffect(() => {
    frame.setActive(!reduced && focused && foreground);
    return () => frame.setActive(false);
  }, [frame, reduced, focused, foreground]);

  const uniforms = useDerivedValue(() => uniformsFor(mixPacked(from.value, to.value, k.value), {
    size, time: time.value, phase: phase.value, level: level.value, radius: z.radius, reduced,
  }));

  if (!EFFECT) return null;
  return (
    <Canvas style={{ width: size, height: size }} pointerEvents="none">
      <Fill>
        <Shader source={EFFECT} uniforms={uniforms} />
      </Fill>
    </Canvas>
  );
}

export const SkiaOrb = memo(SkiaOrbView);
