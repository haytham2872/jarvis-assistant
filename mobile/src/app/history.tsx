// Past chats on the phone (M4): every phone conversation, newest first. Tapping one opens it
// to read; the current conversation is the main screen.
import { router, useFocusEffect } from 'expo-router';
import { useCallback, useState } from 'react';
import { ActivityIndicator, FlatList, Pressable, StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { HttpError, listChats } from '../chat/client';
import type { ChatSummary } from '../chat/types';
import { chatWhen } from '../chat/when';
import { describeFailure } from '../core/api';
import { loadSettings } from '../core/settings';
import { color, font, radius, space } from '../theme/tokens';
import { Backdrop } from '../ui/Backdrop';
import { IconButton } from '../ui/IconButton';

export default function History() {
  const insets = useSafeAreaInsets();
  const [chats, setChats] = useState<ChatSummary[] | null>(null);
  const [error, setError] = useState('');

  useFocusEffect(useCallback(() => {
    let live = true;
    void (async () => {
      const s = await loadSettings();
      if (!s.baseUrl || !s.token) {
        if (live) setError('Not paired yet.');
        return;
      }
      try {
        const { chats: rows } = await listChats({ baseUrl: s.baseUrl, token: s.token });
        if (live) {
          setChats(rows);
          setError('');
        }
      } catch (e) {
        if (live) setError(e instanceof HttpError ? describeFailure(e.status, e.message) : describeFailure(null, String(e)));
      }
    })();
    return () => {
      live = false;
    };
  }, []));

  return (
    <View style={[styles.screen, { paddingTop: insets.top + space.s2 }]}>
      <Backdrop />
      <View style={styles.header}>
        <IconButton symbol="chevron.left" label="Back"
          onPress={() => (router.canGoBack() ? router.back() : router.replace('/'))} />
        <Text style={styles.title}>Chats</Text>
      </View>
      {error ? <Text style={styles.error}>{error}</Text> : null}
      {!chats && !error ? <ActivityIndicator color={color.tMid} style={{ marginTop: space.s5 }} /> : null}
      {chats ? (
        <FlatList
          data={chats}
          keyExtractor={(c) => c.session_id}
          contentContainerStyle={{ padding: space.s4, gap: space.s2, paddingBottom: insets.bottom + space.s4 }}
          ListEmptyComponent={<Text style={styles.empty}>No phone chats yet.</Text>}
          renderItem={({ item }) => (
            <Pressable accessibilityRole="button" onPress={() => router.push(`/chat/${encodeURIComponent(item.session_id)}`)}
              style={({ pressed }) => [styles.row, pressed && { opacity: 0.7 }]}>
              <View style={styles.rowTop}>
                <Text style={styles.when}>{chatWhen(item.last_at)}</Text>
                <Text style={styles.count}>{item.exchanges} {item.exchanges === 1 ? 'exchange' : 'exchanges'}</Text>
              </View>
              <Text style={styles.first} numberOfLines={2}>{item.first_user_text}</Text>
            </Pressable>
          )}
        />
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.bg },
  header: { flexDirection: 'row', alignItems: 'center', gap: space.s3, paddingHorizontal: space.s4, paddingBottom: space.s2 },
  title: { color: color.tHi, fontSize: font.lg, fontWeight: '600' },
  error: { color: color.err, fontSize: font.label, paddingHorizontal: space.s4 },
  empty: { color: color.tMid, fontSize: font.body, textAlign: 'center', marginTop: space.s5 },
  row: {
    gap: space.s1, padding: space.s3, borderRadius: radius.lg + 6, backgroundColor: 'rgba(14,21,33,0.72)',
    borderColor: color.border, borderWidth: StyleSheet.hairlineWidth,
  },
  rowTop: { flexDirection: 'row', justifyContent: 'space-between' },
  when: { color: color.tMid, fontSize: font.label },
  count: { color: color.tDim, fontSize: font.label },
  first: { color: color.tHi, fontSize: font.body, lineHeight: 21 },
});
