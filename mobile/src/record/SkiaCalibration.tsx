// Static native drawing; the enclosing Pressable selects a dot only when a tap is released.
import { Canvas, Circle, DashPathEffect, Line, vec } from '@shopify/react-native-skia';
import { color } from '../theme/tokens';
import type { Geometry } from './model';

export function SkiaCalibration({ geo, width, height }: { geo: Geometry; width: number; height: number }) {
  const [x0, y0, x1, y1] = geo.diagonal;
  return <Canvas style={{ width, height }}>
    {geo.ticks.map((t) => <Line key={t.value} p1={vec(x0, t.y)} p2={vec(x1, t.y)} color={color.surface3} strokeWidth={1} />)}
    <Line p1={vec(x0, y0)} p2={vec(x1, y1)} color={color.tDim} strokeWidth={1}><DashPathEffect intervals={[5, 5]} /></Line>
    {geo.points.map((p) => <Circle key={p.id} cx={p.x} cy={p.y} r={3.5} color={color.stIdle} opacity={.7} />)}
    {geo.bands.flatMap((b, i) => [
      <Line key={`i${i}`} p1={vec(b.x, b.lo)} p2={vec(b.x, b.hi)} color={color.warn} strokeWidth={1.5} />,
      <Line key={`l${i}`} p1={vec(b.x - 4, b.lo)} p2={vec(b.x + 4, b.lo)} color={color.warn} strokeWidth={1.5} />,
      <Line key={`h${i}`} p1={vec(b.x - 4, b.hi)} p2={vec(b.x + 4, b.hi)} color={color.warn} strokeWidth={1.5} />,
      <Circle key={`p${i}`} cx={b.x} cy={b.y} r={4} color={color.warn} />,
    ])}
  </Canvas>;
}
