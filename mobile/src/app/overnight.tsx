import { router, useFocusEffect } from 'expo-router';
import { useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { getOvernight } from '../chat/client';
import type { OvernightItem, OvernightView } from '../chat/types';
import { useCachedView, viewKey } from '../core/useCachedView';
import { Button } from '../goals/Screen';
import { styles as common } from '../goals/styles';
import { saveLooked, useLastLooked } from '../overnight/lastLooked';
import { card, count, dateTime, departure, explain, headline, periods, sinceFor, timeline, type Period } from '../overnight/model';
import { useApp } from '../state/AppContext';
import { color, font, radius, space } from '../theme/tokens';
import { Glass } from '../ui/Glass';
import { ReadFrame } from '../ui/ReadFrame';
import { ReadList } from '../ui/ReadList';

export default function OvernightScreen() {
  const { cfg } = useApp();
  const scope = viewKey(cfg, 'overnight-looked');
  const looked = useLastLooked(scope);
  const [period, setPeriod] = useState<Period>('evening');
  // Anchor a rolling 24-hour request for this visit rather than changing its cache key each render.
  const [openedAt] = useState(() => Date.now() / 1000);
  const since = sinceFor(period, looked.at, openedAt);
  const key = looked.loaded ? viewKey(cfg, `overnight:${period}:${since ?? 'default'}`) : null;
  const { data, error, stale, loaded, reload, updatedAt } = useCachedView<OvernightView>(key,
    (cache) => getOvernight(cfg!, since, cache), { refreshMs: 60_000 });
  const rows = useMemo(() => timeline(data?.items ?? []), [data]);
  const latest = useRef({ scope, data, failed: true, validatedAt: 0 });
  useLayoutEffect(() => { latest.current = { scope, data, failed: !!error || !loaded, validatedAt: (updatedAt ?? 0) / 1000 }; }, [scope, data, error, loaded, updatedAt]);
  useFocusEffect(useCallback(() => {
    const opened = Date.now() / 1000;
    return () => {
      const current = latest.current;
      if (scope && current.scope === scope) {
        const at = departure(opened, Date.now() / 1000, current.data, current.failed, current.validatedAt);
        if (at !== null) void saveLooked(scope, at);
      }
    };
  }, [scope]));
  const insets = useSafeAreaInsets();
  const open = (item: OvernightItem) => {
    if (item.link) router.push({ pathname: '/mind/note', params: item.link });
    else if (item.goal_id) router.push({ pathname: '/goals/[id]', params: { id: item.goal_id } });
    else if (item.kind === 'forecast') router.push('/record');
  };
  return <ReadFrame title="While you slept">
    <ReadList data={rows} keyExtractor={(item) => item.id} initialNumToRender={8} maxToRenderPerBatch={6} windowSize={7}
      contentContainerStyle={[s.content, { paddingBottom: insets.bottom + space.s6 }]} refreshing={false} onRefresh={() => void reload(true)}
      ListHeaderComponent={<View style={s.header}>
        <View style={s.switches}>{periods.map((p) => <Pressable key={p.id} accessibilityRole="button" accessibilityLabel={p.label}
          accessibilityState={{ selected: period === p.id }} onPress={() => setPeriod(p.id)} style={[s.chip, period === p.id && s.active]}>
          <Text style={[s.chipText, period === p.id && s.activeText]}>{p.label}</Text></Pressable>)}</View>
        {!cfg ? <Text style={common.dim}>Pair with JARVIS in Settings first.</Text> : null}
        {error ? <Text style={common.error} accessibilityRole="alert">{explain(error, 'overnight')}</Text> : null}
        {stale ? <Text style={common.dim}>{stale}</Text> : null}
        {cfg && !data && (!loaded || !looked.loaded) ? <ActivityIndicator color={color.stIdle} accessibilityLabel="Loading overnight work" /> : null}
        {period === 'looked' && looked.loaded && looked.at === null ? <Text style={common.dim}>No previous visit saved. Showing last evening.</Text> : null}
        {data ? <Glass tone={data.complete ? 'blue' : 'amber'} style={s.hero}>
          <Text style={s.kicker}>ON HIS OWN TIME</Text><Text style={s.title}>{headline(data, !!error)}</Text>
          <Text style={s.body}>Since {dateTime(data.since)} · {count(data.items.length)}{!data.complete ? ' found' : ''}</Text>
          {!data.items.length && data.complete ? <Text style={common.dim}>Nothing to catch up on. Anything JARVIS finishes will appear here.</Text> : null}
          {data.notes.map((n) => <Text key={n} style={s.warning} accessibilityRole="alert">{n}</Text>)}
        </Glass> : null}
        {data?.items.some((i) => i.date_only) ? <Text style={common.dim}>Some entries record only a day. Their time is unknown and they may predate this window.</Text> : null}
        {error ? <Button label="Try again" onPress={() => void reload(true)} /> : null}
      </View>}
      renderItem={({ item }) => {
        const words = card(item), actionable = !!(item.link || item.goal_id || item.kind === 'forecast');
        const content = <Glass style={s.card}>
          <View style={s.row}><Text style={s.kicker}>{words.label}</Text><Text style={s.clock}>{words.when}</Text></View>
          <Text style={s.cardTitle}>{words.title}</Text>
          <Text style={s.body} selectable={!actionable}>{words.detail}</Text>
          {words.verdict ? <Text style={[common.dim, item.ok === false && s.warning]}>{words.verdict}</Text> : null}
          {actionable ? <Text style={s.link}>{item.link ? 'Read note' : item.goal_id ? 'Open goal' : 'See track record'} ›</Text> : null}
        </Glass>;
        return <View style={s.entry}>{item.hour ? <Text style={s.hour}>{item.hour} · Paris</Text> : null}
          {actionable ? <Pressable accessibilityRole="button" accessibilityLabel={`Open ${words.label}: ${item.title}`} onPress={() => open(item)} style={({ pressed }) => pressed && common.pressed}>{content}</Pressable> : content}</View>;
      }} />
  </ReadFrame>;
}

const s = StyleSheet.create({
  content: { paddingHorizontal: space.s4, paddingTop: space.s2, gap: space.s3 }, header: { gap: space.s3 },
  switches: { flexDirection: 'row', flexWrap: 'wrap', gap: space.s2 },
  chip: { paddingVertical: space.s2, paddingHorizontal: space.s3, borderRadius: radius.pill, backgroundColor: color.surface, borderWidth: 1, borderColor: color.border },
  active: { borderColor: color.stIdle, backgroundColor: color.surface3 }, chipText: { color: color.tMid, fontSize: font.label }, activeText: { color: color.tHi },
  hero: { padding: space.s5, gap: space.s3 }, title: { color: color.tHi, fontSize: 28, lineHeight: 34, fontWeight: '600' },
  kicker: { color: color.tMid, fontSize: font.micro, letterSpacing: .7, flexShrink: 1 },
  body: { color: color.tHi, fontSize: font.body, lineHeight: 23 }, warning: { color: color.warn, fontSize: font.label, lineHeight: 20 },
  entry: { gap: space.s2 }, hour: { color: color.tMid, fontSize: font.label, marginTop: space.s3 },
  card: { padding: space.s4, gap: space.s3 }, cardTitle: { color: color.tHi, fontSize: font.read, lineHeight: 25, fontWeight: '500' },
  row: { flexDirection: 'row', flexWrap: 'wrap', justifyContent: 'space-between', gap: space.s2 }, clock: { color: color.tMid, fontSize: font.micro },
  link: { color: color.stIdle, fontSize: font.label },
});
