import { useIsFocused } from 'expo-router';
import { useEffect, useState } from 'react';
import { AppState, StyleSheet, Text, View } from 'react-native';
import Animated, { cancelAnimation, useAnimatedStyle, useSharedValue, withRepeat, withTiming } from 'react-native-reanimated';
import { useReduceMotion } from '../chat/Orb';
import type { GoalDetail, GoalMilestone } from '../chat/types';
import { color, space } from '../theme/tokens';
import { Button } from './Screen';
import { nodeState } from './model';
import { styles as s } from './styles';

export function MilestonePath({ goal, onFile }: { goal: GoalDetail; onFile: (name: string) => void }) {
  const focused = useIsFocused();
  const reduced = useReduceMotion();
  const [foreground, setForeground] = useState(AppState.currentState === 'active');
  useEffect(() => {
    const sub = AppState.addEventListener('change', (state) => setForeground(state === 'active'));
    return () => sub.remove();
  }, []);
  return <View style={{ gap: space.s1 }}>
    {goal.milestones.map((m, i) => <Node key={m.id} milestone={m} status={nodeState(m, goal)}
      last={i === goal.milestones.length - 1} pulse={focused && foreground && !reduced} onFile={onFile} />)}
  </View>;
}
function Node({ milestone: m, status, last, pulse, onFile }: { milestone: GoalMilestone;
  status: ReturnType<typeof nodeState>; last: boolean; pulse: boolean; onFile: (name: string) => void }) {
  const opacity = useSharedValue(1);
  useEffect(() => {
    cancelAnimation(opacity);
    opacity.value = status === 'working' && pulse ? withRepeat(withTiming(0.4, { duration: 900 }), -1, true) : 1;
    return () => cancelAnimation(opacity);
  }, [opacity, status, pulse]);
  const animated = useAnimatedStyle(() => ({ opacity: opacity.value }));
  const hue = status === 'done' ? color.ok : status === 'failed' ? color.err : status === 'waiting' ? color.warn : color.stIdle;
  return <View style={path.row}>
    <View style={path.rail}><Animated.View style={[path.dot, { borderColor: hue }, animated]} />
      {!last ? <View style={path.line} /> : null}</View>
    <View style={[s.grow, { paddingBottom: space.s5 }]}>
      <Text style={[s.section, { color: hue }]}>{m.id} · {status}</Text><Text style={s.heading}>{m.title}</Text>
      {m.summary ? <Text style={s.dim}>{m.summary}</Text> : null}
      {m.outputs.length ? <View style={s.buttons}>{m.outputs.map((f) =>
        <Button key={`${f.name}:${f.revision}`} label={`Open ${f.name}`} onPress={() => onFile(f.name)} />)}</View> : null}
    </View>
  </View>;
}
const path = StyleSheet.create({
  row: { flexDirection: 'row', gap: space.s3 }, rail: { width: space.s4, alignItems: 'center', paddingTop: space.s1 },
  dot: { width: 12, height: 12, borderWidth: 2, borderRadius: 6, backgroundColor: color.bg },
  line: { flex: 1, width: StyleSheet.hairlineWidth, backgroundColor: color.borderBright, marginTop: space.s1 },
});
