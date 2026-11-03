// The orb (docs/archive/MOBILE-APP-PLAN.md M4): a compact echo of the desktop sphere above the conversation.
// Its colour says what JARVIS is doing; its halo follows the real microphone level while
// The owner records and the real output level while the server's voice plays. Levels arrive
// through a LevelBus and move a native-driven Animated value: nothing here re-renders per
// sample. With Reduce Motion on, nothing loops or grows: the halo only brightens.
import { memo, useEffect, useRef, useState } from 'react';
import { AccessibilityInfo, Animated, Easing, StyleSheet, Text, View } from 'react-native';

import { color, font, space } from '../theme/tokens';
import { ORB_LABEL, type LevelBus, type OrbMode } from '../voice/levels';

export const ORB_COLOR: Record<OrbMode, string> = {
  idle: color.stIdle,
  listening: color.stListening,
  transcribing: color.stListening,
  thinking: color.stThinking,
  speaking: color.stSpeaking,
};

const SIZE = 30;

export function useReduceMotion(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    let alive = true;
    void AccessibilityInfo.isReduceMotionEnabled().then((r) => alive && setReduced(r)).catch(() => undefined);
    const sub = AccessibilityInfo.addEventListener('reduceMotionChanged', setReduced);
    return () => {
      alive = false;
      sub.remove();
    };
  }, []);
  return reduced;
}

type Props = { mode: OrbMode; bus: LevelBus; reduced?: boolean; showLabel?: boolean };

function OrbView({ mode, bus, reduced: forced, showLabel = true }: Props) {
  const system = useReduceMotion();
  const reduced = forced ?? system;
  const level = useRef(new Animated.Value(0)).current;
  const breath = useRef(new Animated.Value(0)).current;
  const live = mode === 'listening' || mode === 'speaking';

  // Real levels, only in the two modes that have one.
  useEffect(() => {
    if (!live) {
      level.setValue(0);
      return;
    }
    return bus.subscribe((v) => {
      Animated.timing(level, { toValue: v, duration: 90, easing: Easing.out(Easing.quad), useNativeDriver: true }).start();
    });
  }, [bus, live, level]);

  // A slow breath while idle, a quicker pulse while thinking or transcribing; none with Reduce Motion.
  useEffect(() => {
    breath.setValue(0);
    if (reduced || live) return;
    const ms = mode === 'idle' ? 3200 : 900;
    const loop = Animated.loop(Animated.sequence([
      Animated.timing(breath, { toValue: 1, duration: ms, easing: Easing.inOut(Easing.sin), useNativeDriver: true }),
      Animated.timing(breath, { toValue: 0, duration: ms, easing: Easing.inOut(Easing.sin), useNativeDriver: true }),
    ]));
    loop.start();
    return () => loop.stop();
  }, [mode, reduced, live, breath]);

  const tint = ORB_COLOR[mode];
  const haloScale = reduced ? 1 : Animated.add(1, Animated.add(Animated.multiply(level, 0.55), Animated.multiply(breath, 0.12)));
  const haloOpacity = Animated.add(0.18, Animated.add(Animated.multiply(level, 0.6), Animated.multiply(breath, 0.25)));
  const label = ORB_LABEL[mode];

  return (
    <View style={styles.row} accessible accessibilityRole="image"
      accessibilityLabel={label ? `JARVIS is ${label.toLowerCase()}` : 'JARVIS is idle'} testID="orb">
      <View style={styles.box}>
        <Animated.View testID="orb-halo"
          style={[styles.halo, { backgroundColor: tint, opacity: haloOpacity, transform: [{ scale: haloScale }] }]} />
        <View style={[styles.core, { backgroundColor: tint }]} />
      </View>
      {showLabel && label ? <Text style={[styles.label, { color: tint }]}>{label}</Text> : null}
    </View>
  );
}

/** Re-renders only when the mode changes; levels never touch React. */
export const Orb = memo(OrbView);

const styles = StyleSheet.create({
  row: { flexDirection: 'row', alignItems: 'center', gap: space.s2 },
  box: { width: SIZE, height: SIZE, alignItems: 'center', justifyContent: 'center' },
  halo: { position: 'absolute', width: SIZE, height: SIZE, borderRadius: SIZE / 2 },
  core: { width: SIZE * 0.52, height: SIZE * 0.52, borderRadius: SIZE, opacity: 0.95 },
  label: { fontSize: font.label, fontWeight: '600', letterSpacing: 0.3 },
});
