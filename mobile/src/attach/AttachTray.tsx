// The files waiting to go with the next message (slice 7): a photo shows its thumbnail, a
// document its name; each says whether it is uploading, ready or refused (and why).
import { SymbolView } from 'expo-symbols';
import { ActivityIndicator, Image, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';

import { color, font, radius, space } from '../theme/tokens';
import type { FileChip, Pending } from './model';

type TrayProps = { items: Pending[]; onRemove: (id: string) => void; onRetry: (id: string) => void };

export function AttachTray({ items, onRemove, onRetry }: TrayProps) {
  if (!items.length) return null;
  return (
    <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.tray}
      keyboardShouldPersistTaps="handled" testID="attach-tray">
      {items.map((p) => (
        <View key={p.id} style={[styles.item, p.state === 'failed' && styles.failed]}>
          <Pressable disabled={p.state !== 'failed'} onPress={() => onRetry(p.id)} style={styles.body}
            accessibilityLabel={`${p.name}, ${p.state === 'failed' ? `not sent: ${p.error}` : p.state}`}
            accessibilityRole={p.state === 'failed' ? 'button' : undefined}
            accessibilityHint={p.state === 'failed' ? 'Try uploading again' : undefined}>
            {p.kind === 'image'
              ? <Image source={{ uri: p.uri }} style={styles.thumb} />
              : <View style={[styles.thumb, styles.docThumb]}>
                  <SymbolView name="doc.text" size={20} tintColor={color.stIdle}
                    fallback={<Text style={styles.docMark}>DOC</Text>} />
                </View>}
            <View style={styles.meta}>
              <Text style={styles.name} numberOfLines={1}>{p.name}</Text>
              {p.state === 'uploading' ? <ActivityIndicator size="small" color={color.tMid} style={styles.spin} />
                : p.state === 'failed' ? <Text style={styles.error} numberOfLines={2}>{p.error}</Text>
                  : <Text style={styles.ready}>Ready</Text>}
            </View>
          </Pressable>
          <Pressable accessibilityRole="button" accessibilityLabel={`Remove ${p.name}`} hitSlop={8}
            onPress={() => onRemove(p.id)} style={styles.remove}>
            <SymbolView name="xmark.circle.fill" size={18} tintColor={color.tMid}
              fallback={<Text style={styles.removeMark}>×</Text>} />
          </Pressable>
        </View>
      ))}
    </ScrollView>
  );
}

/** The files a sent message carried, as small pills under his words. */
export function FileChips({ files }: { files: FileChip[] }) {
  if (!files.length) return null;
  return (
    <View style={styles.chips} testID="file-chips">
      {files.map((f, i) => (
        <View key={`${f.name}-${i}`} style={styles.chip} accessibilityLabel={`${f.kind === 'image' ? 'Photo' : 'Document'}: ${f.name}`}>
          <SymbolView name={f.kind === 'image' ? 'photo' : 'doc.text'} size={12} tintColor={color.stIdle}
            fallback={<Text style={styles.chipMark}>{f.kind === 'image' ? 'IMG' : 'DOC'}</Text>} />
          <Text style={styles.chipText} numberOfLines={1}>{f.name}</Text>
        </View>
      ))}
    </View>
  );
}

const THUMB = 44;

const styles = StyleSheet.create({
  tray: { gap: space.s2, paddingHorizontal: space.s1 },
  item: {
    flexDirection: 'row', alignItems: 'center', maxWidth: 230, borderRadius: radius.md,
    backgroundColor: 'rgba(255,255,255,0.07)', borderWidth: StyleSheet.hairlineWidth,
    borderColor: 'rgba(255,255,255,0.14)', paddingRight: space.s1,
  },
  failed: { borderColor: color.err },
  body: { flexDirection: 'row', alignItems: 'center', gap: space.s2, padding: space.s1, flexShrink: 1 },
  thumb: { width: THUMB, height: THUMB, borderRadius: radius.sm, backgroundColor: 'rgba(0,0,0,0.3)' },
  docThumb: { alignItems: 'center', justifyContent: 'center' },
  docMark: { color: color.stIdle, fontSize: 10, fontWeight: '700' },
  meta: { flexShrink: 1, gap: 2 },
  name: { color: color.tHi, fontSize: font.label, maxWidth: 140 },
  spin: { alignSelf: 'flex-start' },
  ready: { color: color.tDim, fontSize: font.label },
  error: { color: color.err, fontSize: font.label, maxWidth: 150 },
  remove: { padding: space.s1 },
  removeMark: { color: color.tMid, fontSize: 18 },
  chips: { flexDirection: 'row', flexWrap: 'wrap', gap: space.s1, marginTop: space.s1, justifyContent: 'flex-end' },
  chip: {
    flexDirection: 'row', alignItems: 'center', gap: 4, maxWidth: 220, paddingHorizontal: space.s2, paddingVertical: 3,
    borderRadius: 999, backgroundColor: 'rgba(47,182,240,0.12)',
  },
  chipMark: { color: color.stIdle, fontSize: 9, fontWeight: '700' },
  chipText: { color: color.tMid, fontSize: font.label, flexShrink: 1 },
});
