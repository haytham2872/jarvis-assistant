// Pieces the Portfolio tab and a holding's screen share: a labelled figure, and a thesis with how
// far the last close is from each of its levels (moved here from the tab, unchanged).
import { StyleSheet, Text, View } from 'react-native';

import type { Thesis } from '../chat/types';
import { color, font, space } from '../theme/tokens';
import { conditionLine, eventLine, type ConditionLine } from './model';

export const TONE: Record<ConditionLine['tone'], string> = { ok: color.ok, near: color.warn, hit: color.err, neutral: color.tMid };

export function Fact({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.fact}>
      <Text style={styles.factLabel}>{label}</Text>
      <Text style={styles.factValue} selectable>{value}</Text>
    </View>
  );
}

export function ThesisBlock({ thesis: t, currency }: { thesis: Thesis; currency: string }) {
  return (
    <View style={styles.section} testID={`thesis-${t.id}`}>
      <Text style={styles.sectionTitle}>
        {t.source === 'owner' ? 'Your thesis' : 'My analysis'}{t.horizon ? ` · ${t.horizon}` : ''}
      </Text>
      <Text style={styles.body}>{t.why}</Text>
      {t.conditions.map((c) => {
        const l = conditionLine(c, currency);
        return (
          <View key={c.id} style={styles.cond}>
            <View style={styles.condText}>
              <Text style={styles.small}>{l.label}</Text>
              <Text style={[styles.small, { color: TONE[l.tone] }]}>{l.detail}</Text>
            </View>
            {l.progress !== null ? (
              <View style={styles.meter}><View style={[styles.meterFill, { flex: l.progress, backgroundColor: TONE[l.tone] }]} />
                <View style={{ flex: 1 - l.progress }} /></View>
            ) : null}
          </View>
        );
      })}
      {eventLine(t.last_event) ? <Text style={styles.small}>{eventLine(t.last_event)}</Text> : null}
    </View>
  );
}

export const styles = StyleSheet.create({
  body: { color: color.tHi, fontSize: font.body, lineHeight: 21 },
  small: { color: color.tMid, fontSize: font.label, lineHeight: 18 },
  fact: { flexDirection: 'row', justifyContent: 'space-between', gap: space.s3 },
  factLabel: { color: color.tMid, fontSize: font.label },
  factValue: { color: color.tHi, fontSize: font.label, fontVariant: ['tabular-nums'], flexShrink: 1, textAlign: 'right' },
  section: { gap: 4, paddingTop: space.s2 },
  sectionTitle: { color: color.tMid, fontSize: font.micro, fontWeight: '700', letterSpacing: 0.6, textTransform: 'uppercase' },
  cond: { gap: 4, paddingVertical: 2 },
  condText: { flexDirection: 'row', justifyContent: 'space-between', gap: space.s3, flexWrap: 'wrap' },
  meter: { flexDirection: 'row', height: 4, borderRadius: 2, backgroundColor: color.surface3, overflow: 'hidden' },
  meterFill: { borderRadius: 2 },
});
