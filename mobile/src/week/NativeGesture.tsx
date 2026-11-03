// Loaded only with native Worklets/Reanimated. Updates stay on the UI thread; JS hears releases.
import { useMemo, type ReactNode } from 'react';
import { View, type StyleProp, type ViewStyle } from 'react-native';
import { Gesture, GestureDetector } from 'react-native-gesture-handler';
import Animated, { useAnimatedStyle, useSharedValue, withTiming } from 'react-native-reanimated';
import { scheduleOnRN } from 'react-native-worklets';
import { color } from '../theme/tokens';
import { clashes, FIRST, HOUR, snap, type Span } from './model';

export type DragProps = { uid: string; index: number; minute: number; duration: number; width: number;
  bases: number[]; busy: Span[]; buffer: number; lo: number; hi: number; disabled: boolean; reduced: boolean;
  onDrop: (uid: string, index: number, minute: number) => void; onPage: (direction: number) => void;
  children: ReactNode; position?: StyleProp<ViewStyle> };

export function Drag(p: DragProps) {
  const x = useSharedValue(0), y = useSharedValue(0), active = useSharedValue(false), invalid = useSharedValue(false);
  const index = useSharedValue(p.index), minute = useSharedValue(p.minute);
  const { uid, width, bases, busy, buffer, lo, hi, duration, disabled, reduced, onDrop, onPage } = p;
  const origin = p.index, startMinute = p.minute;
  const gesture = useMemo(() => {
    const drag = Gesture.Pan().enabled(!disabled).activateAfterLongPress(350)
      .onStart(() => { active.set(true); })
      .onUpdate((e) => {
        index.set(Math.max(0, Math.min(bases.length - 1, origin + Math.round(e.translationX / width))));
        minute.set(snap(startMinute + e.translationY * 60 / HOUR, duration));
        x.set((index.get() - origin) * width);
        y.set((minute.get() - startMinute) * HOUR / 60);
        const from = bases[index.get()] + (minute.get() - FIRST) * 60;
        invalid.set(minute.get() < lo || minute.get() + duration > hi || clashes(uid, from, from + duration * 60, busy, buffer));
      })
      .onEnd((_e, success) => {
        if (success && (index.get() !== origin || minute.get() !== startMinute)) scheduleOnRN(onDrop, uid, index.get(), minute.get());
      })
      .onFinalize((_e, success) => {
        active.set(false); invalid.set(false);
        x.set(reduced || success ? 0 : withTiming(0, { duration: 160 }));
        y.set(reduced || success ? 0 : withTiming(0, { duration: 160 }));
      });
    const swipe = Gesture.Pan().activeOffsetX([-12, 12]).failOffsetY([-14, 14])
      .onEnd((e, success) => { if (success && Math.abs(e.translationX) > 40) scheduleOnRN(onPage, e.translationX < 0 ? 1 : -1); });
    return Gesture.Race(drag, swipe);
  }, [disabled, bases, origin, width, startMinute, duration, lo, hi, uid, busy, buffer, onDrop, onPage, reduced,
    active, index, minute, x, y, invalid]);
  const style = useAnimatedStyle(() => ({ transform: [{ translateX: x.get() }, { translateY: y.get() }],
    zIndex: active.get() ? 50 : 1, borderColor: invalid.get() ? color.err : active.get() ? color.tHi : 'transparent',
    borderWidth: 2, borderRadius: 9, backgroundColor: invalid.get() ? '#732f3d' : 'transparent' }));
  return <GestureDetector gesture={gesture}><Animated.View style={[p.position, style]}>{p.children}</Animated.View></GestureDetector>;
}

export function EmptyGrid({ children, disabled, onAdd, onPage }: { children: ReactNode; disabled: boolean;
  onAdd: (x: number, y: number) => void; onPage: (n: number) => void }) {
  const gesture = useMemo(() => Gesture.Race(
    Gesture.LongPress().enabled(!disabled).minDuration(350).maxDistance(10)
      .onEnd((e, success) => { if (success) scheduleOnRN(onAdd, e.x, e.y); }),
    Gesture.Pan().activeOffsetX([-12, 12]).failOffsetY([-14, 14])
      .onEnd((e, success) => { if (success && Math.abs(e.translationX) > 40) scheduleOnRN(onPage, e.translationX < 0 ? 1 : -1); }),
  ), [disabled, onAdd, onPage]);
  return <GestureDetector gesture={gesture}><View style={{ flex: 1 }}>{children}</View></GestureDetector>;
}
