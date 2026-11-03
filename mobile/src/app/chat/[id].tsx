// One past phone chat, to read (M4). Answers are formatted and can be read aloud.
import { router, useLocalSearchParams } from 'expo-router';
import { useEffect, useState } from 'react';
import { ActivityIndicator, FlatList, Pressable, StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { getChat, HttpError } from '../../chat/client';
import { Markdown } from '../../chat/Markdown';
import { FileChips } from '../../attach/AttachTray';
import { splitMarker } from '../../attach/model';
import type { Exchange } from '../../chat/types';
import { chatWhen } from '../../chat/when';
import { describeFailure, type ServerConfig } from '../../core/api';
import { loadSettings } from '../../core/settings';
import { backdrop, color, font, radius, space } from '../../theme/tokens';
import { Backdrop } from '../../ui/Backdrop';
import { IconButton } from '../../ui/IconButton';
import { loadPrefs } from '../../voice/prefs';
import { useSpeaker } from '../../voice/useSpeaker';

export default function PastChat() {
  const insets = useSafeAreaInsets();
  const { id } = useLocalSearchParams<{ id: string }>();
  const [cfg, setCfg] = useState<ServerConfig | null>(null);
  const [rate, setRate] = useState(1);
  const [rows, setRows] = useState<Exchange[] | null>(null);
  const [error, setError] = useState('');
  const speaker = useSpeaker(cfg, rate);

  useEffect(() => {
    let live = true;
    void (async () => {
      const s = await loadSettings();
      void loadPrefs().then((p) => live && setRate(p.rate));
      if (!s.baseUrl || !s.token) {
        if (live) setError('Not paired yet.');
        return;
      }
      const c = { baseUrl: s.baseUrl, token: s.token };
      if (live) setCfg(c);
      try {
        const { exchanges } = await getChat(c, String(id));
        if (live) setRows(exchanges);
      } catch (e) {
        if (live) setError(e instanceof HttpError ? describeFailure(e.status, e.message) : describeFailure(null, String(e)));
      }
    })();
    return () => {
      live = false;
    };
  }, [id]);

  return (
    <View style={[styles.screen, { paddingTop: insets.top + space.s2 }]}>
      <Backdrop />
      <View style={styles.header}>
        <IconButton symbol="chevron.left" label="Chats"
          onPress={() => (router.canGoBack() ? router.back() : router.replace('/history'))} />
        <Text style={styles.title}>{rows?.length ? chatWhen(rows[0].at) : 'Chat'}</Text>
      </View>
      {error ? <Text style={styles.error}>{error}</Text> : null}
      {!rows && !error ? <ActivityIndicator color={color.tMid} style={{ marginTop: space.s5 }} /> : null}
      {rows ? (
        <FlatList
          data={rows}
          keyExtractor={(e) => e.id}
          extraData={speaker.state}
          contentContainerStyle={{ padding: space.s4, gap: space.s4, paddingBottom: insets.bottom + space.s4 }}
          renderItem={({ item }) => {
            const source = `ex:${item.id}`;
            const mine = speaker.state.source === source && speaker.state.phase !== 'idle';
            return (
              <View style={styles.pair}>
                {splitMarker(item.user).text ? (
                  <Text style={styles.user} selectable>{splitMarker(item.user).text}</Text>
                ) : null}
                <FileChips files={splitMarker(item.user).files} />
                <Markdown text={item.assistant} />
                <Pressable accessibilityRole="button" accessibilityLabel={mine ? 'Stop reading' : 'Read aloud'} hitSlop={10}
                  onPress={() => speaker.toggle(source, item.assistant)}>
                  <Text style={styles.speak}>{mine ? 'Stop' : 'Read aloud'}</Text>
                </Pressable>
              </View>
            );
          }}
        />
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.bg },
  header: { flexDirection: 'row', alignItems: 'center', gap: space.s3, paddingHorizontal: space.s4, paddingBottom: space.s2 },
  title: { color: color.tHi, fontSize: font.read, fontWeight: '600' },
  error: { color: color.err, fontSize: font.label, paddingHorizontal: space.s4 },
  pair: { gap: space.s2 },
  user: {
    alignSelf: 'flex-end', maxWidth: '85%', color: color.tHi, fontSize: font.body, lineHeight: 21,
    backgroundColor: backdrop.bubble, borderColor: backdrop.bubbleEdge, borderWidth: StyleSheet.hairlineWidth, overflow: 'hidden',
    borderRadius: 18, borderBottomRightRadius: 6, paddingHorizontal: space.s3 + 2, paddingVertical: space.s2,
  },
  speak: { color: color.tMid, fontSize: font.label },
});
