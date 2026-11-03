// What a dragged line (or a typed price) proposes (next plan, slice 3): watch for a close below
// or above it, or add it to one of his open theses as a "wrong if" (or "right if") condition.
// Nothing is recorded until he taps; the server's checks are the ledger's and the watch store's.
import * as Haptics from 'expo-haptics';
import { useEffect, useState } from 'react';
import { Modal, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';

import { proposeCondition, proposeWatch } from '../chat/client';
import type { Thesis } from '../chat/types';
import type { ServerConfig } from '../core/api';
import { color, font, radius, space } from '../theme/tokens';
import { Glass } from '../ui/Glass';
import { proposal } from './chart';

type Props = {
  visible: boolean;
  cfg: ServerConfig;
  symbol: string;
  currency: string;
  last: number | null;
  level: number | null;
  theses: Thesis[];
  explain: (e: unknown) => string;
  onClose: () => void;
  onDone: (text: string) => void;
};

export function ProposalSheet(p: Props) {
  const [text, setText] = useState('');
  const [thesis, setThesis] = useState<number | null>(null);
  const [right, setRight] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!p.visible) return;
    setText(p.level !== null ? String(proposal(p.symbol, p.currency, p.level, p.last)?.level ?? '') : '');
    setThesis(p.theses.length === 1 ? p.theses[0].id : null);
    setRight(false);
    setError('');
  }, [p.visible, p.level, p.last, p.symbol, p.currency, p.theses]);

  const level = Number(text.replace(',', '.'));
  const plan = text.trim() ? proposal(p.symbol, p.currency, level, p.last) : null;

  const run = async (what: 'watch' | 'thesis') => {
    if (!plan || busy) return;
    setBusy(true);
    setError('');
    try {
      const r = what === 'watch'
        ? await proposeWatch(p.cfg, p.symbol, plan.direction, plan.level)
        : await proposeCondition(p.cfg, p.symbol, thesis!, right ? 'right_if' : 'wrong_if', plan.kind, plan.level);
      void Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
      p.onDone(r.text);
    } catch (e) {
      setError(p.explain(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal visible={p.visible} transparent animationType="fade" onRequestClose={p.onClose}>
      <Pressable style={styles.veil} onPress={p.onClose} accessibilityLabel="Close" accessibilityRole="button" />
      <View style={styles.wrap} pointerEvents="box-none">
        <Glass tone="blue" style={styles.sheet} testID="proposal-sheet">
          <Text style={styles.title}>{plan ? `${plan.words[0].toUpperCase()}${plan.words.slice(1)}?` : `A price for ${p.symbol}`}</Text>
          <TextInput accessibilityLabel={`Price in ${p.currency}`} keyboardType="decimal-pad" value={text} onChangeText={setText}
            placeholder={p.last !== null ? `Last close ${p.last}` : 'Price'} placeholderTextColor={color.tDim} style={styles.input} />
          <Text style={styles.dim}>
            {plan ? (plan.direction === 'below' ? 'Below the last close: it watches for a fall.' : 'Above the last close: it watches for a rise.')
              : 'Type a price above zero.'}
          </Text>
          {error ? <Text style={styles.error}>{error}</Text> : null}

          <Pressable accessibilityRole="button" accessibilityLabel={plan ? `Watch ${plan.words}` : 'Watch'} disabled={!plan || busy}
            onPress={() => void run('watch')} style={({ pressed }) => pressed && styles.pressed}>
            <Glass tone="accent" interactive style={[styles.btn, (!plan || busy) && styles.off]}>
              <Text style={styles.btnDark}>Watch</Text>
            </Glass>
          </Pressable>
          <Text style={styles.dim}>Checked once a day; a message when it happens.</Text>

          {p.theses.length ? (
            <View style={styles.group}>
              <Text style={styles.section}>Or add it to a thesis</Text>
              {p.theses.length > 1 ? p.theses.map((t) => (
                <Pressable key={t.id} accessibilityRole="radio" accessibilityState={{ checked: thesis === t.id }}
                  accessibilityLabel={`Thesis: ${t.why}`} onPress={() => setThesis(t.id)}>
                  <Text style={[styles.choice, thesis === t.id && styles.chosen]} numberOfLines={2}>{t.why}</Text>
                </Pressable>
              )) : <Text style={styles.choice} numberOfLines={2}>{p.theses[0].why}</Text>}
              <View style={styles.row}>
                {(['Wrong if', 'Right if'] as const).map((w) => (
                  <Pressable key={w} accessibilityRole="radio" accessibilityState={{ checked: (w === 'Right if') === right }}
                    accessibilityLabel={w} onPress={() => setRight(w === 'Right if')}
                    style={[styles.chip, (w === 'Right if') === right && styles.chipOn]}>
                    <Text style={styles.chipText}>{w}</Text>
                  </Pressable>
                ))}
              </View>
              <Pressable accessibilityRole="button" accessibilityLabel="Add to thesis" disabled={!plan || thesis === null || busy}
                onPress={() => void run('thesis')} style={({ pressed }) => pressed && styles.pressed}>
                <Glass interactive style={[styles.btn, (!plan || thesis === null || busy) && styles.off]}>
                  <Text style={styles.btnText}>Add to thesis</Text>
                </Glass>
              </Pressable>
            </View>
          ) : null}

          <Pressable accessibilityRole="button" onPress={p.onClose}><Text style={styles.cancel}>Cancel</Text></Pressable>
        </Glass>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  veil: { ...StyleSheet.absoluteFill, backgroundColor: 'rgba(0,0,0,0.5)' },
  wrap: { flex: 1, justifyContent: 'flex-end', padding: space.s3 },
  sheet: { borderRadius: 26, padding: space.s4, gap: space.s2 },
  title: { color: color.tHi, fontSize: font.read, fontWeight: '700' },
  input: { color: color.tHi, fontSize: 24, fontWeight: '600', paddingVertical: space.s2, borderBottomColor: color.border,
    borderBottomWidth: StyleSheet.hairlineWidth, fontVariant: ['tabular-nums'] },
  dim: { color: color.tMid, fontSize: font.label, lineHeight: 18 },
  error: { color: color.err, fontSize: font.label, lineHeight: 18 },
  btn: { borderRadius: 18, height: 40, paddingHorizontal: space.s4, justifyContent: 'center', alignItems: 'center', marginTop: space.s1 },
  btnText: { color: color.tHi, fontSize: font.label, fontWeight: '700' },
  btnDark: { color: color.bg, fontSize: font.label, fontWeight: '700' },
  off: { opacity: 0.45 },
  pressed: { opacity: 0.75 },
  group: { gap: space.s2, marginTop: space.s2 },
  section: { color: color.tMid, fontSize: font.micro, fontWeight: '700', letterSpacing: 0.6, textTransform: 'uppercase' },
  choice: { color: color.tMid, fontSize: font.label, padding: space.s2, borderRadius: radius.md, borderWidth: 1, borderColor: color.border },
  chosen: { color: color.tHi, borderColor: color.stIdle },
  row: { flexDirection: 'row', gap: space.s2 },
  chip: { borderRadius: radius.pill, borderWidth: 1, borderColor: color.border, paddingHorizontal: space.s3, height: 30, justifyContent: 'center' },
  chipOn: { borderColor: color.stIdle, backgroundColor: 'rgba(47,182,240,0.15)' },
  chipText: { color: color.tHi, fontSize: font.label, fontWeight: '600' },
  cancel: { color: color.stIdle, fontSize: font.label, textAlign: 'center', paddingTop: space.s2 },
});
