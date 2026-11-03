import { memo } from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { color, font } from '../theme/tokens';
import { skiaAvailable, SkiaBoundary } from '../ui/skia';
import { budgetFraction } from './model';

type Drawing = typeof import('./BudgetDrawing').BudgetDrawing;
let drawing: Drawing | null | undefined;
function loadDrawing() {
  if (drawing !== undefined) return drawing;
  drawing = null;
  if (skiaAvailable()) {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    try { drawing = require('./BudgetDrawing').BudgetDrawing as Drawing; } catch { /* native fallback */ }
  }
  return drawing;
}

export const BudgetRing = memo(function BudgetRing({ spent, budget }: { spent: number | null; budget: number }) {
  const fraction = budgetFraction(spent, budget);
  const Drawing = loadDrawing();
  const fallback = <View style={styles.track}><View style={[styles.fill, { width: `${fraction * 100}%` }]} /></View>;
  return <View accessible accessibilityRole="progressbar" accessibilityLabel="Goal budget used or reserved"
    accessibilityValue={{ min: 0, max: 100, now: Math.round(fraction * 100) }} style={styles.box}>
    {Drawing ? <SkiaBoundary fallback={fallback}><Drawing fraction={fraction} /></SkiaBoundary> : fallback}
    <Text style={styles.label}>{spent === null ? '—' : `${Math.round(fraction * 100)}%`}</Text>
  </View>;
});
const styles = StyleSheet.create({
  box: { width: 48, height: 48, alignItems: 'center', justifyContent: 'center' },
  label: { position: 'absolute', color: color.tMid, fontSize: font.micro, fontWeight: '600' },
  track: { position: 'absolute', bottom: 0, width: 48, height: 3, backgroundColor: color.borderBright, borderRadius: 2, overflow: 'hidden' },
  fill: { height: 3, backgroundColor: color.stIdle },
});
