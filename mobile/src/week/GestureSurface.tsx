import type { ReactNode } from 'react';
import { Pressable, View } from 'react-native';
import { skiaAvailable } from '../ui/skia';
import type { DragProps } from './NativeGesture';

let native: typeof import('./NativeGesture') | null | undefined;
function gestures() {
  if (native === undefined) {
    try { native = skiaAvailable() ? require('./NativeGesture') as typeof import('./NativeGesture') : null; }
    catch { native = null; }
  }
  return native;
}
export function DragBlock(p: DragProps) {
  const Native = gestures()?.Drag;
  return Native ? <Native {...p} /> : <View style={p.position}>{p.children}</View>;
}
export function EmptySurface(p: { children: ReactNode; disabled: boolean; onAdd: (x: number, y: number) => void;
  onPage: (n: number) => void }) {
  const Native = gestures()?.EmptyGrid;
  return Native ? <Native {...p} /> : <Pressable style={{ flex: 1 }} disabled={p.disabled}
    accessible={false} delayLongPress={350} onLongPress={(e) => p.onAdd(e.nativeEvent.locationX, e.nativeEvent.locationY)}>{p.children}</Pressable>;
}
