// His holdings by weight (next plan, slice 3): a ring with the legend beside it, or the bar the
// tab always had where Skia cannot draw.
import { StyleSheet, Text, View } from 'react-native';

import type { Position } from '../chat/types';
import { color, font, space } from '../theme/tokens';
import { skiaAvailable, SkiaBoundary } from '../ui/skia';
import { pct } from './model';

type SkiaRingType = typeof import('./SkiaRing').SkiaRing;
let loaded: SkiaRingType | null | undefined;
function skiaRing(): SkiaRingType | null {
  if (loaded !== undefined) return loaded;
  try {
    loaded = skiaAvailable() ? (require('./SkiaRing') as typeof import('./SkiaRing')).SkiaRing : null;
  } catch {
    loaded = null;
  }
  return loaded;
}

const GAP_DEG = 2.5;

/** Arcs from the top, clockwise: each holding's weight of 360 degrees, less a small gap. */
export function arcsFor(weights: { weight: number; color: string }[]) {
  const total = weights.reduce((s, w) => s + w.weight, 0);
  if (total <= 0) return [];
  let at = -90;
  return weights.map((w) => {
    const full = (w.weight / total) * 360;
    const arc = { start: at + GAP_DEG / 2, sweep: Math.max(0.5, full - GAP_DEG), color: w.color };
    at += full;
    return arc;
  });
}

export function Ring({ positions, palette }: { positions: Position[]; palette: string[] }) {
  const parts = positions.filter((p) => p.weight_pct !== null && Number(p.weight_pct) > 0);
  if (!parts.length) return null;
  const tint = (p: Position) => palette[positions.indexOf(p) % palette.length];
  const Chart = skiaRing();
  const bar = (
    <View style={styles.bar} testID="allocation-bar">
      {parts.map((p) => <View key={p.symbol} style={{ flex: Number(p.weight_pct), backgroundColor: tint(p) }} />)}
    </View>
  );
  return (
    <View style={styles.wrap} accessible
      accessibilityLabel={`Allocation: ${parts.map((p) => `${p.symbol} ${pct(p.weight_pct, false)}`).join(', ')}`}>
      {Chart ? (
        <SkiaBoundary fallback={bar}>
          <Chart size={88} stroke={12} arcs={arcsFor(parts.map((p) => ({ weight: Number(p.weight_pct), color: tint(p) })))} />
        </SkiaBoundary>
      ) : null}
      <View style={Chart ? styles.legendSide : styles.legendBelow}>
        {!Chart ? bar : null}
        <View style={styles.legend}>
          {parts.map((p) => (
            <View key={p.symbol} style={styles.item}>
              <View style={[styles.dot, { backgroundColor: tint(p) }]} />
              <Text style={styles.text}>{p.symbol} {pct(p.weight_pct, false)}</Text>
            </View>
          ))}
        </View>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: { flexDirection: 'row', alignItems: 'center', gap: space.s4 },
  legendSide: { flex: 1 },
  legendBelow: { flex: 1, gap: space.s2 },
  bar: { flexDirection: 'row', height: 8, borderRadius: 4, overflow: 'hidden', gap: 2 },
  legend: { flexDirection: 'row', flexWrap: 'wrap', gap: space.s3 },
  item: { flexDirection: 'row', alignItems: 'center', gap: 5 },
  dot: { width: 8, height: 8, borderRadius: 4 },
  text: { color: color.tMid, fontSize: font.label },
});
