import { useMemo, useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import type { RecordBand, RecordCall } from '../chat/types';
import { color, font } from '../theme/tokens';
import { skiaAvailable, SkiaBoundary } from '../ui/skia';
import { geometry, pct, pick, type Mode } from './model';

type Native = typeof import('./SkiaCalibration').SkiaCalibration;
let drawing: Native | null | undefined;
function nativeDrawing() {
  if (drawing !== undefined) return drawing;
  try { drawing = skiaAvailable() ? (require('./SkiaCalibration') as typeof import('./SkiaCalibration')).SkiaCalibration : null; }
  catch (error) { console.warn('Record chart unavailable; showing the band table.', error); drawing = null; }
  return drawing;
}
export function CalibrationChart({ calls, bands, mode, onPick }: { calls: RecordCall[]; bands: RecordBand[]; mode: Mode; onPick: (id: number) => void }) {
  const [width, setWidth] = useState(0);
  const height = 220, Native = calls.length ? nativeDrawing() : null;
  const geo = useMemo(() => geometry(calls, bands, mode, width || 300, height), [calls, bands, mode, width]);
  const table = <View testID="record-band-table" style={{ gap: 8 }}>{bands.map((b) => <View key={b.lo} style={{ gap: 2 }}>
    <Text style={s.body}>{pct(b.lo)}–{pct(b.hi)}: {b.hits} of {b.n} true</Text>
    <Text style={s.dim}>Said {pct(b.mean_p)} on average · plausible range {pct(b.interval[0])}–{pct(b.interval[1])}</Text>
  </View>)}</View>;
  if (!calls.length) return <Text style={s.dim}>No settled calls to plot yet. Open calls stay below until they settle.</Text>;
  return <View testID="record-chart" style={{ gap: 10 }} onLayout={(e) => { if (e.nativeEvent.layout.width > 0) setWidth(e.nativeEvent.layout.width); }}>
    {Native && width ? <SkiaBoundary fallback={table}>
      <Pressable testID="record-calibration" accessible={false} onPress={(e) => { const id = pick(geo.points, e.nativeEvent.locationX, e.nativeEvent.locationY); if (id !== null) onPick(id); }} style={{ height }}>
        <Native geo={geo} width={width} height={height} />
        {geo.ticks.map((t) => <Text key={`y${t.value}`} style={[s.tick, { left: 0, top: t.y - 7 }]}>{pct(t.value)}</Text>)}
        {geo.ticks.map((t) => <Text key={`x${t.value}`} style={[s.tick, { left: t.x - 12, bottom: 0 }]}>{pct(t.value)}</Text>)}
        {geo.bands.map((b, i) => <Text key={i} style={[s.tick, { right: 0, top: b.labelY, color: color.warn }]}>{b.label}</Text>)}
      </Pressable>
      <Text style={s.dim}>Across: probability · up: how often it happened. Blue: calls (tap a dot). Amber: band means and 95% intervals. The diagonal is perfect calibration.</Text>
    </SkiaBoundary> : table}
  </View>;
}
const s = StyleSheet.create({ body: { color: color.tHi, fontSize: font.label }, dim: { color: color.tMid, fontSize: font.label, lineHeight: 19 },
  tick: { position: 'absolute', color: color.tMid, fontSize: 10, fontVariant: ['tabular-nums'] } });
