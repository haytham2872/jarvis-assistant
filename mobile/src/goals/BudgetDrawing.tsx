import { Canvas, Circle, Path, Skia } from '@shopify/react-native-skia';
import { color } from '../theme/tokens';

export function BudgetDrawing({ fraction }: { fraction: number }) {
  const path = Skia.PathBuilder.Make().addArc({ x: 3, y: 3, width: 42, height: 42 }, -90, fraction * 360).detach();
  return <Canvas style={{ width: 48, height: 48 }}>
    <Circle cx={24} cy={24} r={21} color={color.borderBright} style="stroke" strokeWidth={3} />
    {fraction > 0 ? <Path path={path} color={fraction >= 0.8 ? color.warn : color.stIdle} style="stroke" strokeWidth={3} strokeCap="round" /> : null}
  </Canvas>;
}
