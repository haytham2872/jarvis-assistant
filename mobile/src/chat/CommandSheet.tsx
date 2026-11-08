// The command list (the ⌘ button in the composer): every slash command with a line on what it
// does; one with fixed choices opens them as chips. Tapping runs it, the composer shows the reply.
import { useEffect, useState } from 'react';
import { Modal, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';

import { describeFailure, ExplainedError, type ServerConfig } from '../core/api';
import { color, font, radius, space } from '../theme/tokens';
import { Glass } from '../ui/Glass';
import { getCommands, type CommandInfo } from './client';

type Props = {
  visible: boolean;
  cfg: ServerConfig;
  onClose: () => void;
  /** The full command text to run: "/about" or "/pace low". */
  onRun: (text: string) => void;
};

/** A failure in words (an HttpError carries a status; a dropped connection does not). */
export function commandError(e: unknown): string {
  if (e instanceof ExplainedError) return e.message;
  const status = (e as { status?: unknown })?.status;
  const detail = e instanceof Error ? e.message : String(e);
  return typeof status === 'number' ? describeFailure(status, detail) : describeFailure(null, detail);
}

export function CommandSheet(p: Props) {
  const [commands, setCommands] = useState<CommandInfo[] | null>(null);
  const [error, setError] = useState('');
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    if (!p.visible) return;
    setOpen(null);
    setError('');
    let live = true;
    getCommands(p.cfg).then(
      (list) => live && setCommands(list),
      (e) => live && setError(commandError(e)),
    );
    return () => { live = false; };
  }, [p.visible, p.cfg]);

  return (
    <Modal visible={p.visible} transparent animationType="fade" onRequestClose={p.onClose}>
      <Pressable style={styles.veil} onPress={p.onClose} accessibilityLabel="Close" accessibilityRole="button" />
      <View style={styles.wrap} pointerEvents="box-none">
        <Glass tone="blue" style={styles.sheet} testID="command-sheet">
          <Text style={styles.title}>Commands</Text>
          {error ? <Text style={styles.error}>{error}</Text> : null}
          {!error && !commands ? <Text style={styles.dim}>Loading…</Text> : null}
          <ScrollView style={styles.list} keyboardShouldPersistTaps="handled">
            {(commands ?? []).map((c) => (
              <View key={c.name} style={styles.item}>
                <Pressable accessibilityRole="button" accessibilityLabel={c.name} style={({ pressed }) => [styles.row, pressed && styles.pressed]}
                  onPress={() => (c.choices.length ? setOpen(open === c.name ? null : c.name) : p.onRun(c.name))}>
                  <Text style={styles.name}>{c.name}</Text>
                  <Text style={styles.dim} numberOfLines={1}>{c.description}</Text>
                </Pressable>
                {open === c.name ? (
                  <View style={styles.chips}>
                    {c.choices.map((choice) => (
                      <Pressable key={choice} accessibilityRole="button" accessibilityLabel={choice} style={styles.chip}
                        onPress={() => p.onRun(`${c.name} ${choice}`)}>
                        <Text style={styles.chipText}>{choice}</Text>
                      </Pressable>
                    ))}
                  </View>
                ) : null}
              </View>
            ))}
          </ScrollView>
          <Pressable accessibilityRole="button" onPress={p.onClose}><Text style={styles.cancel}>Close</Text></Pressable>
        </Glass>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  veil: { ...StyleSheet.absoluteFill, backgroundColor: 'rgba(0,0,0,0.5)' },
  wrap: { flex: 1, justifyContent: 'flex-end', padding: space.s3 },
  sheet: { borderRadius: 26, padding: space.s4, gap: space.s2, maxHeight: '75%' },
  title: { color: color.tHi, fontSize: font.read, fontWeight: '700' },
  list: { flexGrow: 0 },
  item: { borderBottomColor: color.border, borderBottomWidth: StyleSheet.hairlineWidth },
  row: { paddingVertical: space.s2, gap: 2 },
  name: { color: color.tHi, fontSize: font.body, fontWeight: '700' },
  dim: { color: color.tMid, fontSize: font.label, lineHeight: 18 },
  error: { color: color.err, fontSize: font.label, lineHeight: 18 },
  chips: { flexDirection: 'row', flexWrap: 'wrap', gap: space.s2, paddingBottom: space.s2 },
  chip: { borderRadius: radius.pill, borderWidth: 1, borderColor: color.stIdle, backgroundColor: 'rgba(47,182,240,0.15)',
    paddingHorizontal: space.s3, height: 30, justifyContent: 'center' },
  chipText: { color: color.tHi, fontSize: font.label, fontWeight: '600' },
  pressed: { opacity: 0.75 },
  cancel: { color: color.stIdle, fontSize: font.label, textAlign: 'center', paddingTop: space.s2 },
});
