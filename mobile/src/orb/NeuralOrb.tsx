// The orb as the app shows it (slice 6): the GPU neural orb when this build of Expo Go has Skia,
// Reanimated and Worklets, and the earlier Animated orb otherwise (the web preview, an Expo Go
// without them, a shader that fails to compile, or a render that throws). Either way it carries
// the same label for VoiceOver, and the orb on screen buzzes on the state changes that matter.
import { memo } from 'react';
import { StyleSheet, View } from 'react-native';
import { skiaAvailable, SkiaBoundary as Boundary } from '../ui/skia';

import { Orb, useReduceMotion } from '../chat/Orb';
import { ORB_LABEL, type LevelBus, type OrbMode } from '../voice/levels';
import { useOrbHaptics } from './haptics';
import type { Mood } from './states';

const FALLBACK_PX = 30;           // the Animated orb's own size; it is scaled to fit

type SkiaOrbType = typeof import('./SkiaOrb').SkiaOrb;
let loaded: SkiaOrbType | null | undefined;

/** The GPU orb, or null when it cannot run here. Probed once: requiring Skia without its native
 *  module would throw (it looks the module up with getEnforcing). */
export function gpuOrb(): SkiaOrbType | null {
  if (loaded !== undefined) return loaded;
  loaded = null;
  if (!skiaAvailable()) return null;
  try {
    const mod = require('./SkiaOrb') as typeof import('./SkiaOrb');
    loaded = mod.EFFECT ? mod.SkiaOrb : null;
  } catch {
    loaded = null;
  }
  return loaded;
}

/** For tests: probe again. */
export function _resetGpuOrb() {
  loaded = undefined;
}

type Props = {
  mode: OrbMode;
  bus: LevelBus;
  size: number;
  mood?: Mood;
  /** Only the orb on screen buzzes (the header's is off while conversation mode covers it). */
  haptics?: boolean;
  reduced?: boolean;
};

function NeuralOrbView({ mode, bus, size, mood, haptics = false, reduced: forced }: Props) {
  const system = useReduceMotion();
  const reduced = forced ?? system;
  useOrbHaptics(mode, haptics);
  const Gpu = gpuOrb();
  const label = ORB_LABEL[mode];
  const fallback = (
    <View style={{ transform: [{ scale: size / FALLBACK_PX * 0.8 }] }} testID="orb-fallback"
      aria-hidden>
      <Orb mode={mode} bus={bus} reduced={reduced} showLabel={false} />
    </View>
  );
  return (
    <View style={[styles.box, { width: size, height: size }]} accessible accessibilityRole="image"
      accessibilityLabel={label ? `JARVIS is ${label.toLowerCase()}` : 'JARVIS is idle'} testID="neural-orb">
      {Gpu ? (
        <Boundary fallback={fallback}>
          <Gpu mode={mode} bus={bus} size={size} mood={mood} reduced={reduced} />
        </Boundary>
      ) : fallback}
    </View>
  );
}

export const NeuralOrb = memo(NeuralOrbView);

const styles = StyleSheet.create({
  box: { alignItems: 'center', justifyContent: 'center' },
});
