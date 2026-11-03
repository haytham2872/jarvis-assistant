// Activity (slice 1): what JARVIS is running, what waits on the owner, what is scheduled and what
// he did in the last day, the same view as /activity on Discord (backend/brain/activity.py), and
// the switch that stops his own work, the same as /pause and /resume.
import { useFocusEffect } from 'expo-router';
import * as Haptics from 'expo-haptics';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Pressable, RefreshControl, ScrollView, StyleSheet, Switch, Text, View } from 'react-native';
import { SafeAreaView as TabSafeArea } from 'react-native-screens/experimental';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { getActivity, pauseWork, resumeWork } from '../../chat/client';
import type { ActivitySection, ActivityView } from '../../chat/types';
import { describeFailure, ExplainedError } from '../../core/api';
import { useCachedView, viewKey } from '../../core/useCachedView';
import { useApp } from '../../state/AppContext';
import { HubTiles } from '../../hub/HubTiles';
import { color, font, radius, space } from '../../theme/tokens';
import { Backdrop } from '../../ui/Backdrop';
import { Glass } from '../../ui/Glass';

const REFRESH_MS = 30_000;
export const PAUSE_CHOICES: { label: string; hours: number | null }[] = [
  { label: '1 hour', hours: 1 }, { label: '3 hours', hours: 3 }, { label: 'Until I resume', hours: null },
];

function clock(ts: number): string {
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function explain(e: unknown): string {
  if (e instanceof ExplainedError) return e.message;
  const err = e as { status?: number; message?: string };
  return describeFailure(typeof err?.status === 'number' ? err.status : null, err?.message ?? '');
}

export default function Activity() {
  const insets = useSafeAreaInsets();
  const { cfg, waitingCount } = useApp();
  const { data: view, error: loadError, stale, reload: load, setData: setView } = useCachedView<ActivityView>(
    viewKey(cfg, 'activity'), (cache) => getActivity(cfg!, cache), { refreshMs: REFRESH_MS });
  const [actionError, setError] = useState('');
  const error = actionError || (loadError ? explain(loadError) : '');
  const [said, setSaid] = useState('');
  const [busy, setBusy] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [holdAll, setHoldAll] = useState(false);
  const focused = useRef(false);

  useFocusEffect(useCallback(() => {
    focused.current = true;
    return () => {
      focused.current = false;
    };
  }, [load]));

  // An approval decided or arriving elsewhere changes "waiting for you": read it again. Only on
  // a change: opening the tab already reads it (useFocusEffect).
  const seenWaiting = useRef(waitingCount);
  useEffect(() => {
    if (seenWaiting.current === waitingCount) return;
    seenWaiting.current = waitingCount;
    if (focused.current) void load();
  }, [waitingCount, load]);

  const act = async (run: () => Promise<{ text: string; activity: ActivityView }>) => {
    if (busy || !cfg) return;
    setBusy(true);
    try {
      const r = await run();
      setView(r.activity);
      setSaid(r.text);
      setError('');
      void Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
    } catch (e) {
      setError(explain(e));
    } finally {
      setBusy(false);
    }
  };

  const onRefresh = async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  };

  return (
    <View style={styles.screen}>
      <Backdrop />
      <TabSafeArea edges={{ bottom: true }} style={styles.fill}>
        <ScrollView contentInsetAdjustmentBehavior="never"
          contentContainerStyle={[styles.content, { paddingTop: insets.top + space.s3 }]}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={() => void onRefresh()} tintColor={color.stIdle} />}>
          <View style={styles.titleRow}>
            <Text style={styles.title}>Activity</Text>
            {view ? <Text style={styles.asOf}>as of {clock(view.as_of)}</Text> : null}
          </View>

          {!cfg ? <Text style={styles.dim}>Pair with JARVIS in Settings first.</Text> : null}
          {error ? <Text style={styles.error}>{error}</Text> : null}
          {stale ? <Text style={styles.dim}>{stale}</Text> : null}
          <HubTiles />

          {cfg ? (
            <PauseCard paused={view?.paused ?? null} busy={busy} holdAll={holdAll} setHoldAll={setHoldAll}
              onPause={(hours) => void act(() => pauseWork(cfg, hours, holdAll))}
              onResume={() => void act(() => resumeWork(cfg))} said={said} />
          ) : null}

          {view?.sections.map((s) => <Section key={s.title} section={s} />)}
        </ScrollView>
      </TabSafeArea>
    </View>
  );
}

function PauseCard(p: {
  paused: string | null; busy: boolean; holdAll: boolean; setHoldAll: (v: boolean) => void;
  onPause: (hours: number | null) => void; onResume: () => void; said: string;
}) {
  return (
    <Glass tone="blue" style={styles.pauseCard} testID="pause-card">
      {p.paused ? (
        <>
          <Text style={styles.cardTitle}>Paused</Text>
          <Text style={styles.body}>{p.paused}</Text>
          <Pressable accessibilityRole="button" onPress={p.onResume} disabled={p.busy}
            style={({ pressed }) => [pressed && styles.pressed, p.busy && styles.off]}>
            <Glass tone="accent" interactive style={styles.primary}><Text style={styles.primaryText}>Resume</Text></Glass>
          </Pressable>
        </>
      ) : (
        <>
          <Text style={styles.cardTitle}>Pause my own work</Text>
          <Text style={styles.dim}>
            Research, watches, forecast checks, scheduled and night tasks and new background jobs stop.
            {p.holdAll ? ' Reminders and the morning brief wait too.' : ' Reminders and the morning brief still come.'}
          </Text>
          <View style={styles.chips}>
            {PAUSE_CHOICES.map((c) => (
              <Pressable key={c.label} accessibilityRole="button" accessibilityLabel={`Pause for ${c.label}`}
                onPress={() => p.onPause(c.hours)} disabled={p.busy}
                style={({ pressed }) => [pressed && styles.pressed, p.busy && styles.off]}>
                <Glass interactive style={styles.chip}><Text style={styles.chipText}>{c.label}</Text></Glass>
              </Pressable>
            ))}
          </View>
          <View style={styles.switchRow}>
            <Text style={[styles.body, styles.grow]}>Hold reminders and the brief too</Text>
            <Switch value={p.holdAll} onValueChange={p.setHoldAll} trackColor={{ true: color.stIdle, false: color.surface3 }}
              accessibilityLabel="Hold reminders and the brief too" />
          </View>
        </>
      )}
      {p.said ? <Text style={styles.said}>{p.said}</Text> : null}
    </Glass>
  );
}

function Section({ section }: { section: ActivitySection }) {
  const n = section.items.length;
  return (
    <View style={styles.section}>
      <View style={styles.sectionHead}>
        <Text style={styles.sectionTitle}>{section.title}</Text>
        {n ? <Text style={styles.count}>{n}</Text> : null}
      </View>
      {n ? section.items.map((line, i) => (
        <View key={i} style={styles.item}>
          <View style={styles.bullet} />
          <Text style={styles.body} selectable>{line}</Text>
        </View>
      )) : <Text style={styles.dim}>{section.empty.charAt(0).toUpperCase() + section.empty.slice(1)}</Text>}
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.bg },
  fill: { flex: 1 },
  content: { paddingHorizontal: space.s4, paddingBottom: space.s6, gap: space.s4 },
  titleRow: { flexDirection: 'row', alignItems: 'baseline', justifyContent: 'space-between' },
  title: { color: color.tHi, fontSize: 32, fontWeight: '700', letterSpacing: 0.3 },
  asOf: { color: color.tDim, fontSize: font.label },
  pauseCard: { borderRadius: 22, padding: space.s4, gap: space.s3 },
  cardTitle: { color: color.tHi, fontSize: font.read, fontWeight: '600' },
  body: { flex: 1, color: color.tHi, fontSize: font.body, lineHeight: 21 },
  grow: { flex: 1 },
  dim: { color: color.tMid, fontSize: font.label, lineHeight: 19 },
  error: { color: color.err, fontSize: font.label },
  said: { color: color.stIdle, fontSize: font.label, lineHeight: 19 },
  chips: { flexDirection: 'row', flexWrap: 'wrap', gap: space.s2 },
  chip: { borderRadius: 18, height: 36, paddingHorizontal: space.s4, justifyContent: 'center' },
  chipText: { color: color.tHi, fontSize: font.label, fontWeight: '600' },
  switchRow: { flexDirection: 'row', alignItems: 'center', gap: space.s3 },
  primary: { alignSelf: 'flex-start', borderRadius: 20, height: 40, paddingHorizontal: space.s5, justifyContent: 'center' },
  primaryText: { color: color.bg, fontSize: font.body, fontWeight: '700' },
  section: {
    gap: space.s2, padding: space.s4, borderRadius: radius.lg + 6, backgroundColor: 'rgba(14,21,33,0.72)',
    borderColor: color.border, borderWidth: StyleSheet.hairlineWidth,
  },
  sectionHead: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  sectionTitle: { color: color.tMid, fontSize: font.label, fontWeight: '700', letterSpacing: 0.6, textTransform: 'uppercase' },
  count: { color: color.stIdle, fontSize: font.label, fontWeight: '700' },
  item: { flexDirection: 'row', gap: space.s3, alignItems: 'flex-start' },
  bullet: { width: 6, height: 6, borderRadius: 3, backgroundColor: color.stIdle, marginTop: 8 },
  off: { opacity: 0.45 },
  pressed: { opacity: 0.75 },
});
