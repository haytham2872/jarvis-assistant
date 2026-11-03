// A portfolio line chart (next plan, slice 3): works out every coordinate (chart.ts), lays the
// labels over the drawing as ordinary text, and draws with Skia when the app has it. Without
// Skia (the web, tests, a device where drawing fails) it falls back to the column chart the tab
// always had, and the levels stay readable as text below it.
import { useMemo, useState } from 'react';
import { StyleSheet, Text, View, type LayoutChangeEvent } from 'react-native';

import { color, font } from '../theme/tokens';
import { skiaAvailable, SkiaBoundary } from '../ui/skia';
import { bandQuads, indexOn, scale, spread } from './chart';
import { heights, money } from './model';

export type Level = { value: number; label: string; color: string };
export type Marker = { day: string; value: number; color: string; ring?: boolean };
export type Vertical = { day: string; color: string };

type Props = {
  days: string[];
  values: (number | null)[];
  cost?: (number | null)[];
  currency: string;
  height: number;
  tint: string;
  averageCost?: number | null;
  levels?: Level[];
  markers?: Marker[];
  verticals?: Vertical[];
  onScrub?: (i: number | null) => void;
  /** Hold, drag up or down, let go: the level the line was left at. */
  onPropose?: (level: number) => void;
  accessibilityLabel: string;
  testID?: string;
};

type SkiaChart = typeof import('./SkiaLineChart').SkiaLineChart;
let loaded: SkiaChart | null | undefined;

/** The Skia chart, or null where it cannot run. Probed once, before importing it. */
function skiaChart(): SkiaChart | null {
  if (loaded !== undefined) return loaded;
  try {
    loaded = skiaAvailable() ? (require('./SkiaLineChart') as typeof import('./SkiaLineChart')).SkiaLineChart : null;
  } catch {
    loaded = null;
  }
  return loaded;
}

const RIGHT = 8;
const PAD = 12;

export function LineChart(p: Props) {
  const [width, setWidth] = useState(0);
  const [dragY, setDragY] = useState<number | null>(null);
  const onLayout = (e: LayoutChangeEvent) => setWidth(Math.round(e.nativeEvent.layout.width));
  const Chart = skiaChart();

  const geo = useMemo(() => {
    if (!width) return null;
    const f = { width, height: p.height, top: PAD, bottom: PAD, right: RIGHT };
    const extra = [...(p.levels ?? []).map((l) => l.value), ...(p.averageCost ? [p.averageCost] : [])];
    const s = scale(p.values, f, extra);
    const ys = p.values.map((v) => (v === null ? null : s.y(v)));
    const cost = p.cost?.map((v) => (v === null ? null : s.y(v)));
    const band = cost ? bandQuads(s.xs, ys, cost) : undefined;
    const lines = [
      ...(p.averageCost ? [{ y: s.y(p.averageCost), color: color.tMid, label: `avg ${money(String(p.averageCost), p.currency)}` }] : []),
      ...(p.levels ?? []).map((l) => ({ y: s.y(l.value), color: l.color, label: l.label })),
    ];
    const labelYs = spread(lines.map((l) => l.y - 14), 15, 0, p.height - 14);
    const dots = (p.markers ?? []).flatMap((m) => {
      const i = indexOn(p.days, m.day);
      return i < 0 ? [] : [{ x: s.xs[i], y: s.y(m.value), color: m.color, ring: m.ring }];
    });
    const vlines = (p.verticals ?? []).flatMap((v) => {
      const i = indexOn(p.days, v.day);
      return i < 0 || v.day > p.days[p.days.length - 1] ? [] : [{ x: s.xs[i], color: v.color }];
    });
    return { s, ys, cost, band, lines, labelYs, dots, vlines };
  }, [width, p.height, p.values, p.cost, p.levels, p.averageCost, p.markers, p.verticals, p.days, p.currency]);

  const fallback = <Bars values={p.values} height={p.height} tint={p.tint} />;
  const dragValue = geo && dragY !== null ? geo.s.value(dragY) : null;

  return (
    <View onLayout={onLayout} style={{ height: p.height }} testID={p.testID}
      accessible accessibilityLabel={p.accessibilityLabel}>
      {geo && Chart && p.values.length > 1 ? (
        <SkiaBoundary fallback={fallback}>
          <Chart width={width} height={p.height} xs={geo.s.xs} ys={geo.ys} cost={geo.cost} band={geo.band}
            tint={p.tint} costTint="rgba(232,244,255,0.45)" gainTint="rgba(70,194,138,0.16)" lossTint="rgba(229,96,77,0.16)"
            hlines={geo.lines.map((l) => ({ y: l.y, color: l.color, dashed: true }))} vlines={geo.vlines} dots={geo.dots}
            onScrub={p.onScrub} onDrag={p.onPropose ? setDragY : undefined}
            onRelease={p.onPropose ? (y) => p.onPropose?.(geo.s.value(y)) : undefined} />
          {geo.lines.map((l, i) => (
            <Text key={i} style={[styles.label, { top: geo.labelYs[i], color: l.color }]} numberOfLines={1}
              importantForAccessibility="no">{l.label}</Text>
          ))}
          {dragValue !== null ? (
            <Text style={[styles.drag, { top: Math.max(0, (dragY ?? 0) - 22) }]} importantForAccessibility="no">
              {money(String(dragValue), p.currency)}
            </Text>
          ) : null}
        </SkiaBoundary>
      ) : fallback}
    </View>
  );
}

/** The column chart the tab always had: one bar per value, scaled between the lowest and highest. */
function Bars({ values, height, tint }: { values: (number | null)[]; height: number; tint: string }) {
  const hs = heights(values.map((v) => (v === null ? null : String(v))));
  if (hs.length < 2) return null;
  return (
    <View style={[styles.bars, { height }]} testID="chart-fallback" accessibilityElementsHidden importantForAccessibility="no-hide-descendants">
      {hs.map((h, i) => (
        <View key={i} style={[styles.bar, { height: Math.max(2, h * height), backgroundColor: tint,
          opacity: 0.35 + 0.65 * (i / (hs.length - 1)) }]} />
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  label: { position: 'absolute', right: RIGHT + 2, fontSize: font.micro, fontWeight: '600', backgroundColor: 'rgba(3,10,20,0.55)', paddingHorizontal: 4, borderRadius: 4 },
  drag: { position: 'absolute', left: 6, fontSize: font.label, fontWeight: '700', color: '#ffb347', backgroundColor: 'rgba(3,10,20,0.75)', paddingHorizontal: 6, borderRadius: 6 },
  bars: { flexDirection: 'row', alignItems: 'flex-end', gap: 1.5 },
  bar: { flex: 1, borderRadius: 1.5 },
});
