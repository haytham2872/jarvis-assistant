// The portfolio line, on the GPU (next plan, slice 3). Draws coordinates LineChart has already
// worked out; labels are ordinary text laid over it there. Two gestures on the UI thread: slide a
// finger sideways to read a day (scrub), or hold still, then move up or down to drag a level
// line and let go to propose it. Loaded only when Skia, Reanimated and Worklets are in the app.
import { Canvas, Circle, DashPathEffect, Group, Line, Path, Skia, vec } from '@shopify/react-native-skia';
import { memo, useMemo } from 'react';
import { Gesture, GestureDetector } from 'react-native-gesture-handler';
import { useDerivedValue, useSharedValue } from 'react-native-reanimated';
import { scheduleOnRN } from 'react-native-worklets';

import type { Quad } from './chart';

export type Dot = { x: number; y: number; color: string; r?: number; ring?: boolean };
export type HLine = { y: number; color: string; dashed?: boolean };
export type VLine = { x: number; color: string };

export type DrawProps = {
  width: number;
  height: number;
  xs: number[];
  ys: (number | null)[];
  cost?: (number | null)[];
  band?: { gain: Quad[]; loss: Quad[] };
  tint: string;
  costTint: string;
  gainTint: string;
  lossTint: string;
  hlines: HLine[];
  vlines: VLine[];
  dots: Dot[];
  onScrub?: (i: number | null) => void;
  onDrag?: (y: number | null) => void;
  onRelease?: (y: number) => void;
};

function linePath(xs: number[], ys: (number | null)[]) {
  const p = Skia.PathBuilder.Make();
  let pen = false;
  ys.forEach((y, i) => {
    if (y === null) { pen = false; return; }
    if (pen) p.lineTo(xs[i], y);
    else { p.moveTo(xs[i], y); pen = true; }
  });
  return p.detach();
}

function quadsPath(quads: Quad[]) {
  const p = Skia.PathBuilder.Make();
  for (const q of quads) {
    p.moveTo(q[0][0], q[0][1]);
    for (let k = 1; k < q.length; k++) p.lineTo(q[k][0], q[k][1]);
    p.close();
  }
  return p.detach();
}

function SkiaLineChartView(p: DrawProps) {
  const line = useMemo(() => linePath(p.xs, p.ys), [p.xs, p.ys]);
  const cost = useMemo(() => (p.cost ? linePath(p.xs, p.cost) : null), [p.xs, p.cost]);
  const gain = useMemo(() => (p.band ? quadsPath(p.band.gain) : null), [p.band]);
  const loss = useMemo(() => (p.band ? quadsPath(p.band.loss) : null), [p.band]);

  const scrubX = useSharedValue(-1);
  const scrubIndex = useSharedValue(-1);
  const dragY = useSharedValue(-1);
  const xs = p.xs;
  const { onScrub, onDrag, onRelease, height } = p;

  const gesture = useMemo(() => {
    const scrub = Gesture.Pan()
      .enabled(!!onScrub)
      .activeOffsetX([-8, 8])
      .failOffsetY([-14, 14])
      .onUpdate((e) => {
        // nearest point, on the UI thread
        let best = 0;
        for (let i = 1; i < xs.length; i++) if (Math.abs(xs[i] - e.x) < Math.abs(xs[best] - e.x)) best = i;
        scrubX.set(xs[best] ?? -1);
        if (best !== scrubIndex.value) {
          scrubIndex.set(best);
          if (onScrub) scheduleOnRN(onScrub, best);
        }
      })
      .onFinalize(() => {
        scrubX.set(-1);
        scrubIndex.set(-1);
        if (onScrub) scheduleOnRN(onScrub, null);
      });
    const drag = Gesture.Pan()
      .enabled(!!onRelease)
      .activateAfterLongPress(350)
      .onStart((e) => {
        dragY.set(Math.max(0, Math.min(height, e.y)));
        if (onDrag) scheduleOnRN(onDrag, dragY.value);
      })
      .onUpdate((e) => {
        dragY.set(Math.max(0, Math.min(height, e.y)));
        if (onDrag) scheduleOnRN(onDrag, dragY.value);
      })
      .onEnd(() => {
        if (onRelease && dragY.value >= 0) scheduleOnRN(onRelease, dragY.value);
      })
      .onFinalize(() => {
        dragY.set(-1);
        if (onDrag) scheduleOnRN(onDrag, null);
      });
    return Gesture.Race(drag, scrub);
  }, [xs, onScrub, onDrag, onRelease, height, scrubX, scrubIndex, dragY]);

  const scrubTop = useDerivedValue(() => vec(scrubX.value, 0));
  const scrubBottom = useDerivedValue(() => vec(scrubX.value, height));
  const scrubOpacity = useDerivedValue(() => (scrubX.value < 0 ? 0 : 1));
  const dragLeft = useDerivedValue(() => vec(0, dragY.value));
  const dragRight = useDerivedValue(() => vec(p.width, dragY.value));
  const dragOpacity = useDerivedValue(() => (dragY.value < 0 ? 0 : 1));

  return (
    <GestureDetector gesture={gesture}>
      <Canvas style={{ width: p.width, height: p.height }}>
        {gain ? <Path path={gain} color={p.gainTint} /> : null}
        {loss ? <Path path={loss} color={p.lossTint} /> : null}
        {cost ? (
          <Path path={cost} color={p.costTint} style="stroke" strokeWidth={1.5}>
            <DashPathEffect intervals={[5, 4]} />
          </Path>
        ) : null}
        {p.hlines.map((h, i) => (
          <Line key={`h${i}`} p1={vec(0, h.y)} p2={vec(p.width, h.y)} color={h.color} strokeWidth={1}>
            {h.dashed ? <DashPathEffect intervals={[6, 5]} /> : null}
          </Line>
        ))}
        {p.vlines.map((v, i) => (
          <Line key={`v${i}`} p1={vec(v.x, 0)} p2={vec(v.x, p.height)} color={v.color} strokeWidth={1}>
            <DashPathEffect intervals={[3, 4]} />
          </Line>
        ))}
        <Path path={line} color={p.tint} style="stroke" strokeWidth={2.2} strokeJoin="round" strokeCap="round" />
        {p.dots.map((d, i) => (
          <Group key={`d${i}`}>
            <Circle cx={d.x} cy={d.y} r={(d.r ?? 4) + 1.5} color="rgba(3,10,20,0.85)" />
            <Circle cx={d.x} cy={d.y} r={d.r ?? 4} color={d.color} style={d.ring ? 'stroke' : 'fill'} strokeWidth={2} />
          </Group>
        ))}
        <Line p1={scrubTop} p2={scrubBottom} color="rgba(232,244,255,0.7)" strokeWidth={1} opacity={scrubOpacity} />
        <Line p1={dragLeft} p2={dragRight} color="#ffb347" strokeWidth={1.5} opacity={dragOpacity}>
          <DashPathEffect intervals={[8, 5]} />
        </Line>
      </Canvas>
    </GestureDetector>
  );
}

export const SkiaLineChart = memo(SkiaLineChartView);
