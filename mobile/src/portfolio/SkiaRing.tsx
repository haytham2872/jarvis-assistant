// The allocation ring on the GPU (next plan, slice 3): one arc per holding, by its share of the
// total, with a small gap between arcs. Loaded only when Skia is in the app (see Ring).
import { Canvas, Path, Skia } from '@shopify/react-native-skia';
import { memo, useMemo } from 'react';

export type Arc = { start: number; sweep: number; color: string };

function SkiaRingView({ size, stroke, arcs }: { size: number; stroke: number; arcs: Arc[] }) {
  const paths = useMemo(() => arcs.map((a) => {
    const inset = stroke / 2;
    const p = Skia.PathBuilder.Make()
      .addArc({ x: inset, y: inset, width: size - stroke, height: size - stroke }, a.start, a.sweep)
      .detach();
    return { p, color: a.color };
  }), [arcs, size, stroke]);
  return (
    <Canvas style={{ width: size, height: size }}>
      {paths.map((x, i) => <Path key={i} path={x.p} color={x.color} style="stroke" strokeWidth={stroke} strokeCap="butt" />)}
    </Canvas>
  );
}

export const SkiaRing = memo(SkiaRingView);
