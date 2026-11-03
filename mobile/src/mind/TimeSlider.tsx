import { useState } from 'react';
import { View } from 'react-native';
import { color } from '../theme/tokens';
import { mapDate, timeAt } from './model';

/** A sibling of the map's GestureDetector, never a child: scrubbing cannot pan it. */
export function TimeSlider({ start, end, value, onChange }: { start: number; end: number; value: number; onChange: (at: number) => void }) {
  const [width, setWidth] = useState(1);
  const move = (x: number) => onChange(timeAt(x, width, start, end));
  return <View testID="mind-time-slider" accessible accessibilityRole="adjustable" accessibilityLabel="Map date"
    accessibilityValue={{ min: start, max: end, now: value, text: mapDate(value) }} accessibilityActions={[{ name: 'increment' }, { name: 'decrement' }]}
    onAccessibilityAction={(e) => onChange(Math.max(start, Math.min(end, value + (e.nativeEvent.actionName === 'increment' ? 1 : -1) * Math.max(86400, (end - start) / 30))))}
    onLayout={(e) => setWidth(e.nativeEvent.layout.width)} style={{ height: 44, justifyContent: 'center' }}
    onStartShouldSetResponder={() => true} onMoveShouldSetResponder={() => true} onResponderTerminationRequest={() => false}
    onResponderGrant={(e) => move(e.nativeEvent.locationX)} onResponderMove={(e) => move(e.nativeEvent.locationX)}>
    <View style={{ height: 3, backgroundColor: color.borderBright, borderRadius: 2 }}>
      <View style={{ position: 'absolute', height: 3, width: `${Math.max(0, Math.min(1, (value - start) / Math.max(1, end - start))) * 100}%`, backgroundColor: color.stIdle }} />
      <View style={{ position: 'absolute', top: -7, left: `${Math.max(0, Math.min(1, (value - start) / Math.max(1, end - start))) * 100}%`, marginLeft: -8, width: 17, height: 17, borderRadius: 9, backgroundColor: color.stIdle }} />
    </View>
  </View>;
}
