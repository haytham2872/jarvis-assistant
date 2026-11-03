import { BlurMask, Canvas, Circle, Group, Path, Skia, Text as SkiaText, matchFont } from '@shopify/react-native-skia';
import { useEffect, useMemo, useState } from 'react';
import { Pressable, Text, View } from 'react-native';
import { Gesture, GestureDetector } from 'react-native-gesture-handler';
import { useDerivedValue, useSharedValue, type SharedValue } from 'react-native-reanimated';
import { scheduleOnRN } from 'react-native-worklets';
import { color } from '../theme/tokens';
import { LABEL_FONT, placeLabels, stepScale, zoomStep, type Label } from './labels';
import type { MapProps } from './MindMap';
import { fit, hitTest, pinch, type Point, type Transform } from './layout';
import { folders, glow, matches, visibleGraph } from './model';

/** Labels keep a readable screen size while their anchors follow the UI-thread matrix.
 * A dark outline under the text keeps it legible over lines and dots. */
function MapLabel({ point, label, transform, font }: { point: Point; label: Label; transform: SharedValue<Transform>;
  font: ReturnType<typeof matchFont> }) {
  const width = useMemo(() => font.measureText?.(label.text)?.width ?? label.text.length * 6.6, [font, label.text]);
  const side = label.side;
  const x = useDerivedValue(() => {
    const { scale, x: tx } = transform.value, cx = point.x * scale + tx, r = point.r * scale;
    return side === 'right' ? cx + r + 3 : side === 'left' ? cx - r - 3 - width : cx - width / 2;
  });
  const y = useDerivedValue(() => {
    const { scale, y: ty } = transform.value, cy = point.y * scale + ty, r = point.r * scale;
    return side === 'below' ? cy + r + LABEL_FONT + 2 : side === 'above' ? cy - r - 5 : cy + LABEL_FONT * .35;
  });
  return <Group>
    <SkiaText x={x} y={y} text={label.text} font={font} color={color.bg} style="stroke" strokeWidth={3} strokeJoin="round" />
    <SkiaText x={x} y={y} text={label.text} font={font} color={label.dim ? color.tMid : color.tHi} />
  </Group>;
}

export function SkiaMindMap(props: MapProps & { points: Point[] }) {
  const [frame, setFrame] = useState({ width: 0, height: 0 });
  const transform = useSharedValue<Transform>({ x: 0, y: 0, scale: 1 });
  const panStart = useSharedValue<Transform>({ x: 0, y: 0, scale: 1 }), pinchStart = useSharedValue<Transform>({ x: 0, y: 0, scale: 1 });
  const anchor = useSharedValue({ x: 0, y: 0 }), pinching = useSharedValue(false), moved = useSharedValue(false);
  const lastPan = useSharedValue({ x: 0, y: 0 });
  const [step, setStep] = useState(0), [zoomed, setZoomed] = useState(false);
  const matrix = useDerivedValue(() => {
    const { x, y, scale } = transform.value;
    return [scale, 0, x, 0, scale, y, 0, 0, 1];
  });
  const graph = useMemo(() => visibleGraph(props.view, props.hidden, props.until), [props.view, props.hidden, props.until]);
  const points = useMemo(() => { const ids = new Set(graph.nodes.map((n) => n.id)); return props.points.filter((p) => ids.has(p.id)); },
    [graph.nodes, props.points]);
  const byId = useMemo(() => new Map(points.map((p) => [p.id, p])), [points]);
  const font = useMemo(() => matchFont({ fontFamily: 'Helvetica', fontSize: LABEL_FONT }), []);
  const labels = useMemo(() => placeLabels(points, graph.nodes, stepScale(step), { query: props.query }), [points, graph.nodes, step, props.query]);
  // One path per kind of connection: hundreds of separate lines cost a draw call each.
  const paths = useMemo(() => {
    const link = Skia.PathBuilder.Make(), mention = Skia.PathBuilder.Make();
    for (const e of graph.edges) {
      const a = byId.get(e.a), b = byId.get(e.b);
      if (!a || !b) continue;
      const p = e.kind === 'mention' ? mention : link;
      p.moveTo(a.x, a.y); p.lineTo(b.x, b.y);
    }
    return { link: link.detach(), mention: mention.detach() };
  }, [graph.edges, byId]);
  const showAll = () => {
    if (!frame.width) return;
    const t = fit(frame.width, frame.height, props.points);
    transform.set(t); setStep(zoomStep(t.scale)); moved.set(false); setZoomed(false);
  };
  // Fit the notes as they settle, until he moves the map himself.
  useEffect(() => { if (frame.width && !moved.value) showAll(); }, [frame.width, frame.height, props.points]); // eslint-disable-line react-hooks/exhaustive-deps
  const settle = (scale: number) => { setStep(zoomStep(scale)); setZoomed(true); };
  const select = (x: number, y: number, t: Transform) => { const id = hitTest(points, { x, y }, t); if (id) props.onPick(id); };
  const pan = Gesture.Pan().minDistance(8).maxPointers(1)
    .onStart(() => { panStart.set(transform.value); moved.set(true); })
    .onUpdate((e) => {
      lastPan.set({ x: e.translationX, y: e.translationY });
      if (!pinching.value) transform.set({ ...panStart.value, x: panStart.value.x + e.translationX, y: panStart.value.y + e.translationY });
    })
    .onEnd(() => { scheduleOnRN(settle, transform.value.scale); });
  const zoom = Gesture.Pinch().onStart((e) => {
    pinching.set(true); moved.set(true); pinchStart.set(transform.value); anchor.set({ x: e.focalX, y: e.focalY });
  }).onUpdate((e) => {
    const t = pinch(pinchStart.value, anchor.value, e.scale);
    transform.set({ ...t, x: t.x + e.focalX - anchor.value.x, y: t.y + e.focalY - anchor.value.y });
  }).onFinalize(() => {
    // If one finger stays down, continue from the new zoom rather than the old pan origin.
    panStart.set({ ...transform.value, x: transform.value.x - lastPan.value.x, y: transform.value.y - lastPan.value.y });
    pinching.set(false);
    scheduleOnRN(settle, transform.value.scale);
  });
  const tap = Gesture.Tap().maxDistance(6).maxDuration(280).onEnd((e, success) => {
    if (success && !pinching.value) scheduleOnRN(select, e.x, e.y, transform.value);
  });
  const gesture = Gesture.Race(Gesture.Simultaneous(pan, zoom), tap);
  return <View testID="mind-map-surface" style={{ flex: 1, minHeight: 220 }} onLayout={(e) => {
    const { width, height } = e.nativeEvent.layout;
    if (width !== frame.width || height !== frame.height) setFrame({ width, height });
  }}>
    <GestureDetector gesture={gesture}><Canvas style={{ flex: 1 }} accessible={false}>
      <Group matrix={matrix}>
        <Path path={paths.link} style="stroke" strokeWidth={1.2} color="#6f8fb3" opacity={.32} />
        <Path path={paths.mention} style="stroke" strokeWidth={1.2} color="#9a86d9" opacity={.45} />
        {graph.nodes.map((n) => {
          const p = byId.get(n.id); if (!p) return null;
          const tint = folders[n.folder]?.color ?? color.stIdle, light = glow(n.updated, props.until, props.reduced), found = matches(n, props.query);
          return <Group key={n.id}>
            {light > 0 ? <Circle cx={p.x} cy={p.y} r={p.r + 7} color={tint} opacity={light * .45}><BlurMask blur={5} style="normal" /></Circle> : null}
            {found ? <Circle cx={p.x} cy={p.y} r={p.r + 5} color={color.tHi} style="stroke" strokeWidth={2} /> : null}
            <Circle cx={p.x} cy={p.y} r={p.r} color={tint} opacity={n.size > 0 || found ? 1 : .7} />
          </Group>;
        })}
      </Group>
      {labels.map((l) => {
        const p = byId.get(l.id); return p ? <MapLabel key={l.id} point={p} label={l} transform={transform} font={font} /> : null;
      })}
    </Canvas></GestureDetector>
    {zoomed ? <Pressable accessibilityRole="button" accessibilityLabel="Show the whole map" onPress={showAll} hitSlop={8}
      style={{ position: 'absolute', right: 12, bottom: 12, paddingHorizontal: 12, minHeight: 36, justifyContent: 'center', borderRadius: 18,
        backgroundColor: color.bgElev, borderWidth: 1, borderColor: color.borderBright }}>
      <Text style={{ color: color.tHi, fontSize: 13 }}>Show all</Text></Pressable> : null}
  </View>;
}
