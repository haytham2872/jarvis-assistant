// Inbox (slice 4): every message JARVIS sent the owner on his own -- the brief, reminders, scheduled
// checks, watch alerts, research findings, goal results, the check-in -- newest first, grouped by
// day (GET /inbox, kept by backend/brain/notices.py whatever his delivery setting). "Ask about
// this" opens the chat with that message attached, so "why?" means that message.
import { router, useFocusEffect } from 'expo-router';
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ActivityIndicator, FlatList, Pressable, RefreshControl, ScrollView, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView as TabSafeArea } from 'react-native-screens/experimental';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { getInbox } from '../../chat/client';
import { Markdown } from '../../chat/Markdown';
import type { InboxView, Notice } from '../../chat/types';
import { describeFailure, ExplainedError } from '../../core/api';
import { useCachedView, viewKey } from '../../core/useCachedView';
import {
  groups, isLong, merge, newestId, oldestId, replyParams, speechSource, timeOf, todaysBrief, tone, usedFilters,
  type Filter,
} from '../../inbox/model';
import { kindLabel, ROUTES } from '../../notify/plan';
import { useApp } from '../../state/AppContext';
import { color, font, radius, space } from '../../theme/tokens';
import { Backdrop } from '../../ui/Backdrop';
import { Glass } from '../../ui/Glass';

function explain(e: unknown): string {
  if (e instanceof ExplainedError) return e.message;
  const err = e as { status?: number; message?: string };
  if (err?.status === 404) return 'The server is older than this app: deploy it to see the Inbox.';
  return describeFailure(typeof err?.status === 'number' ? err.status : null, err?.message ?? '');
}

type Row = { type: 'day'; key: string; title: string } | { type: 'notice'; key: string; notice: Notice };

export default function Inbox() {
  const insets = useSafeAreaInsets();
  const { cfg, notices, speaker } = useApp();
  const { pushToken, markRead, unread, route: routeValue } = notices;
  const key = viewKey(cfg, `inbox:${pushToken ?? ''}`);
  const keyRef = useRef(key);
  useLayoutEffect(() => { keyRef.current = key; }, [key]);
  const { data: view, error: loadError, stale, loaded: fetched, reload: load } = useCachedView<InboxView>(
    key, (cache) => getInbox(cfg!, pushToken, {}, cache), { refreshMs: 60_000 });
  const [pages, setPages] = useState<{ key: string | null; items: Notice[] }>({ key, items: [] });
  const items = merge(pages.key === key ? pages.items : [], view?.notices ?? []);
  const loaded = fetched || !!view;
  const error = loadError ? explain(loadError) : '';
  const [filter, setFilter] = useState<Filter>('all');
  const [open, setOpen] = useState<Record<number, boolean>>({});
  const [refreshing, setRefreshing] = useState(false);
  const [more, setMore] = useState(true);
  // What was new when he opened the tab stays marked new for this visit, though it is marked
  // read on the server at once (the badge clears; the dots say what arrived).
  const [fresh, setFresh] = useState<Set<number>>(new Set());
  const loadingMore = useRef(false);

  useEffect(() => {
    if (view) {
      setFresh((had) => {
        const unseen = view.notices.filter((n) => !n.read).map((n) => n.id);
        return unseen.length ? new Set([...had, ...unseen]) : had;
      });
      setMore(view.notices.length >= 50);
      const newest = newestId(view.notices);
      if (newest && view.unread) void markRead(newest);
    }
  }, [view, markRead]);

  // Read on opening, and again whenever a new message is announced while the tab is open.
  useFocusEffect(useCallback(() => {
    void load();
  }, [load, unread]));
  useFocusEffect(useCallback(() => () => setFresh(new Set()), []));   // leaving the tab: all seen

  const loadMore = async () => {
    if (!cfg || !more || loadingMore.current || !items.length) return;
    loadingMore.current = true;
    try {
      const view = await getInbox(cfg, pushToken, { before: oldestId(items) });
      if (keyRef.current !== key) return;
      setPages({ key, items: merge(items, view.notices) });
      setMore(view.notices.length >= 50);
    } catch {
      /* the next scroll tries again */
    } finally {
      loadingMore.current = false;
    }
  };

  const onRefresh = async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  };

  const rows: Row[] = groups(items, filter).flatMap((g) => [
    { type: 'day' as const, key: `day-${g.key}`, title: g.title },
    ...g.items.map((n) => ({ type: 'notice' as const, key: `n-${n.id}`, notice: n })),
  ]);
  const filters = usedFilters(items);
  const route = ROUTES.find((r) => r.value === routeValue);

  const ask = (n: Notice) => router.navigate({ pathname: '/', params: replyParams(n) });
  const brief = todaysBrief(items);
  const playing = (n: Notice) => speaker.state.source === speechSource(n) && speaker.state.phase !== 'idle';

  return (
    <View style={styles.screen}>
      <Backdrop />
      <TabSafeArea edges={{ bottom: true }} style={styles.fill}>
        <FlatList
          data={rows}
          keyExtractor={(r) => r.key}
          contentInsetAdjustmentBehavior="never"
          contentContainerStyle={[styles.content, { paddingTop: insets.top + space.s3 }]}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={() => void onRefresh()} tintColor={color.stIdle} />}
          onEndReachedThreshold={0.4}
          onEndReached={() => void loadMore()}
          ListHeaderComponent={(
            <View style={styles.header}>
              <View style={styles.titleRow}>
                <Text style={styles.title}>Inbox</Text>
                <Pressable accessibilityRole="button" accessibilityLabel="Where JARVIS's messages go"
                  onPress={() => router.push('/settings')}>
                  <Text style={styles.routeLink}>{route?.value === 'discord' ? 'Discord only' : route?.value === 'both' ? 'Discord + phone' : 'Phone only'}</Text>
                </Pressable>
              </View>
              {!cfg ? <Text style={styles.dim}>Pair with JARVIS in Settings first.</Text> : null}
              {brief ? (
                <Pressable accessibilityRole="button" accessibilityLabel={playing(brief) ? 'Stop the brief' : 'Play the brief'}
                  onPress={() => speaker.toggle(speechSource(brief), brief.text)}>
                  <Glass tone="accent" interactive style={styles.play}>
                    <Text style={styles.playText}>{playing(brief) ? '■  Stop the brief' : '▶  Play the brief'}</Text>
                  </Glass>
                </Pressable>
              ) : null}
              {error ? <Text style={styles.error}>{error}</Text> : null}
              {stale ? <Text style={styles.dim}>{stale}</Text> : null}
              {filters.length > 2 ? (
                <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.chips}>
                  {filters.map((f) => (
                    <Pressable key={f.value} accessibilityRole="button" accessibilityState={{ selected: filter === f.value }}
                      onPress={() => setFilter(f.value)}>
                      <Glass tone={filter === f.value ? 'accent' : 'clear'} interactive style={styles.chip}>
                        <Text style={[styles.chipText, filter === f.value && styles.chipTextOn]}>{f.label}</Text>
                      </Glass>
                    </Pressable>
                  ))}
                </ScrollView>
              ) : null}
            </View>
          )}
          ListEmptyComponent={cfg && loaded && !error ? (
            <View style={styles.empty}>
              <Text style={styles.emptyTitle}>Nothing yet</Text>
              <Text style={styles.dim}>
                The brief, reminders, scheduled checks, alerts and research findings land here as JARVIS sends them.
              </Text>
            </View>
          ) : cfg && !loaded ? <ActivityIndicator color={color.tMid} /> : undefined}
          renderItem={({ item }) => item.type === 'day'
            ? <Text style={styles.day}>{item.title}</Text>
            : (
              <NoticeCard notice={item.notice} isNew={!item.notice.read || fresh.has(item.notice.id)}
                playing={playing(item.notice)}
                onPlay={() => speaker.toggle(speechSource(item.notice), item.notice.text)}
                open={!!open[item.notice.id]}
                onToggle={() => setOpen((o) => ({ ...o, [item.notice.id]: !o[item.notice.id] }))}
                onAsk={() => ask(item.notice)} />
            )}
        />
      </TabSafeArea>
    </View>
  );
}

export function NoticeCard({ notice: n, isNew, playing, onPlay, open, onToggle, onAsk }: {
  notice: Notice; isNew: boolean; playing: boolean; onPlay: () => void; open: boolean; onToggle: () => void; onAsk: () => void;
}) {
  const long = isLong(n);
  const accent = tone(n.kind);
  return (
    <View style={styles.card} testID={`notice-${n.id}`}>
      <View style={[styles.accent, { backgroundColor: accent }]} />
      <View style={styles.cardBody}>
        <View style={styles.cardHead}>
          {isNew ? <View style={styles.unread} accessible accessibilityLabel="New" testID={`new-${n.id}`} /> : null}
          <Text style={[styles.kind, { color: accent }]}>{kindLabel(n.kind)}</Text>
          <Text style={styles.time}>{timeOf(n.ts)}</Text>
          <Pressable accessibilityRole="button" accessibilityLabel={playing ? `Stop reading this ${kindLabel(n.kind)}` : `Read this ${kindLabel(n.kind)} aloud`}
            onPress={onPlay} hitSlop={10}>
            <Text style={[styles.speak, playing && styles.speakOn]}>{playing ? '■' : '▶'}</Text>
          </Pressable>
        </View>
        <View style={long && !open ? styles.folded : undefined}>
          <Markdown text={n.text} />
        </View>
        {n.discord === false ? <Text style={styles.warn}>Could not be sent to Discord; it is only here.</Text> : null}
        <View style={styles.actions}>
          {long ? (
            <Pressable accessibilityRole="button" onPress={onToggle}>
              <Text style={styles.link}>{open ? 'Show less' : 'Show all'}</Text>
            </Pressable>
          ) : null}
          {n.kind === 'checkin' ? (
            <Pressable accessibilityRole="button" onPress={() => router.navigate('/today')}>
              <Text style={styles.link}>Open Today</Text>
            </Pressable>
          ) : null}
          <Pressable accessibilityRole="button" accessibilityLabel={`Ask about this ${kindLabel(n.kind)}`} onPress={onAsk}>
            <Glass tone="blue" interactive style={styles.ask}><Text style={styles.askText}>Ask about this</Text></Glass>
          </Pressable>
        </View>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.bg },
  fill: { flex: 1 },
  content: { paddingHorizontal: space.s4, paddingBottom: space.s6, gap: space.s3 },
  header: { gap: space.s3, marginBottom: space.s1 },
  titleRow: { flexDirection: 'row', alignItems: 'baseline', justifyContent: 'space-between' },
  title: { color: color.tHi, fontSize: 32, fontWeight: '700', letterSpacing: 0.3 },
  routeLink: { color: color.stIdle, fontSize: font.label, fontWeight: '600' },
  dim: { color: color.tMid, fontSize: font.label, lineHeight: 19 },
  error: { color: color.err, fontSize: font.label },
  warn: { color: color.warn, fontSize: font.micro },
  chips: { gap: space.s2, paddingVertical: space.s1 },
  chip: { borderRadius: 16, height: 32, paddingHorizontal: space.s3, justifyContent: 'center' },
  chipText: { color: color.tHi, fontSize: font.label, fontWeight: '600' },
  chipTextOn: { color: color.bg },
  day: {
    color: color.tMid, fontSize: font.label, fontWeight: '700', letterSpacing: 0.6, textTransform: 'uppercase',
    marginTop: space.s3,
  },
  card: {
    flexDirection: 'row', borderRadius: radius.lg + 6, overflow: 'hidden', backgroundColor: 'rgba(14,21,33,0.78)',
    borderColor: color.border, borderWidth: StyleSheet.hairlineWidth,
  },
  accent: { width: 3 },
  cardBody: { flex: 1, padding: space.s4, gap: space.s2 },
  cardHead: { flexDirection: 'row', alignItems: 'center', gap: space.s2 },
  unread: { width: 8, height: 8, borderRadius: 4, backgroundColor: color.stIdle },
  kind: { flex: 1, fontSize: font.label, fontWeight: '700', letterSpacing: 0.3 },
  time: { color: color.tDim, fontSize: font.label },
  folded: { maxHeight: 156, overflow: 'hidden' },
  actions: { flexDirection: 'row', alignItems: 'center', justifyContent: 'flex-end', gap: space.s4, marginTop: space.s1 },
  link: { color: color.stIdle, fontSize: font.label, fontWeight: '600' },
  ask: { borderRadius: 16, height: 32, paddingHorizontal: space.s3, justifyContent: 'center' },
  askText: { color: color.tHi, fontSize: font.label, fontWeight: '600' },
  empty: { gap: space.s2, paddingTop: space.s5 },
  play: { alignSelf: 'flex-start', borderRadius: 20, height: 40, paddingHorizontal: space.s5, justifyContent: 'center' },
  playText: { color: color.bg, fontSize: font.body, fontWeight: '700' },
  speak: { color: color.tMid, fontSize: font.label, paddingLeft: space.s2 },
  speakOn: { color: color.stIdle },
  emptyTitle: { color: color.tHi, fontSize: font.read, fontWeight: '600' },
});
