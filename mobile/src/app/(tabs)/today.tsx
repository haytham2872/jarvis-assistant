// Today (slice 2): the owner's day on one screen. His planned blocks in time order with his calendar,
// timed checks and reminders around them and a line where now is; one tap says what became of a
// block (the record the evening check-in makes, so it no longer asks), a skip comes back with a
// new slot to approve on the card, yesterday's blocks stay open until 04:00, and the week shows
// as dots (backend/mobile/today.py).
import { useFocusEffect } from 'expo-router';
import * as Haptics from 'expo-haptics';
import { SymbolView, type SymbolViewProps } from 'expo-symbols';
import { useCallback, useState } from 'react';
import { Pressable, RefreshControl, ScrollView, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView as TabSafeArea } from 'react-native-screens/experimental';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { decideApproval, getToday, HttpError, tapBlock } from '../../chat/client';
import type { Outcome, TodayBlock, TodayView, WeekDay } from '../../chat/types';
import { useCachedView, viewKey } from '../../core/useCachedView';
import { describeFailure, ExplainedError } from '../../core/api';
import { useApp } from '../../state/AppContext';
import {
  checkinLine, choices, clock, OUTCOME_LABEL, phase, tally, timeline, weekDots, when, type Choice, type Dot, type Row,
} from '../../today/model';
import { color, font, radius, space } from '../../theme/tokens';
import { Backdrop } from '../../ui/Backdrop';
import { Glass } from '../../ui/Glass';
import { WeekView } from '../../week/WeekView';
import { ViewSwitch } from '../../week/ViewSwitch';

const REFRESH_MS = 60_000;

const OUTCOME_COLOR: Record<Dot, string> = {
  done: color.ok, partly: color.warn, skipped: color.err, moved: color.stIdle, unknown: color.tDim, open: color.surface3,
};

const KIND_COLOR: Record<string, string> = { work: color.stIdle, gym: color.ok, reading: color.stThinking };
const kindColor = (kind: string) => KIND_COLOR[kind] ?? color.stListening;

function explain(e: unknown): string {
  if (e instanceof ExplainedError) return e.message;
  if (e instanceof HttpError && e.status === 409) return e.message;          // why the block can't take it
  if (e instanceof HttpError && e.status === 404) {
    return "This server doesn't have the Today view yet. Update it and restart JARVIS.";
  }
  const err = e as { status?: number; message?: string };
  return describeFailure(typeof err?.status === 'number' ? err.status : null, err?.message ?? '');
}

type Note = { uid: string | null; text: string; error: boolean };

export default function Today() {
  const [mode, setMode] = useState<'Day' | 'Week'>('Day');
  return mode === 'Week' ? <WeekView onDay={() => setMode('Day')} /> : <Day onWeek={() => setMode('Week')} />;
}

function Day({ onWeek }: { onWeek: () => void }) {
  const insets = useSafeAreaInsets();
  const { cfg } = useApp();
  const { data: view, error: loadError, stale, reload, setData: setView } = useCachedView<TodayView>(
    viewKey(cfg, 'today'), (cache) => getToday(cfg!, cache), { refreshMs: REFRESH_MS });
  const error = loadError ? explain(loadError) : '';
  const [now, setNow] = useState(() => Date.now() / 1000);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<Note | null>(null);
  const [open, setOpen] = useState<string | null>(null);          // a told block whose choices are shown again
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    setNow(Date.now() / 1000);
    await reload();
  }, [reload]);

  useFocusEffect(useCallback(() => {
    setNow(Date.now() / 1000);
    const t = setInterval(() => setNow(Date.now() / 1000), REFRESH_MS);
    return () => clearInterval(t);
  }, [load]));

  const tap = async (b: TodayBlock, outcome: Choice) => {
    if (!cfg || busy) return;
    void Haptics.selectionAsync();
    setBusy(b.uid);
    try {
      const r = await tapBlock(cfg, b.uid, outcome);
      setView(r.today);
      setOpen(null);
      setNote(r.note ? { uid: b.uid, text: r.note, error: false } : null);   // the card shows the rest
      void Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
    } catch (e) {
      setNote({ uid: b.uid, text: explain(e), error: true });
    } finally {
      setBusy(null);
    }
  };

  const decide = async (b: TodayBlock, approve: boolean) => {
    if (!cfg || busy || !b.offer) return;
    setBusy(b.uid);
    try {
      const r = await decideApproval(cfg, b.offer.id, approve);
      setNote({ uid: b.uid, text: r.text, error: false });           // (at the top once a move takes the card off today)
      void Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
      await reload(true);
    } catch (e) {
      setNote({ uid: b.uid, text: explain(e), error: true });
    } finally {
      setBusy(null);
    }
  };

  const onRefresh = async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  };

  const day = view ? timeline(view, now) : null;
  const count = view ? tally(view.blocks) : null;
  const shownUids = new Set([...(view?.blocks ?? []), ...(view?.yesterday.blocks ?? [])].map((b) => b.uid));
  const card = (b: TodayBlock) => (
    <BlockCard key={b.uid} block={b} now={now} busy={busy === b.uid} disabled={!!busy}
      expanded={open === b.uid || !b.outcome} onToggle={() => setOpen(open === b.uid ? null : b.uid)}
      note={note?.uid === b.uid ? note : null} onTap={(o) => void tap(b, o)} onDecide={(a) => void decide(b, a)} />
  );

  return (
    <View style={styles.screen}>
      <Backdrop />
      <TabSafeArea edges={{ bottom: true }} style={styles.fill}>
        <ScrollView contentInsetAdjustmentBehavior="never"
          contentContainerStyle={[styles.content, { paddingTop: insets.top + space.s3 }]}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={() => void onRefresh()} tintColor={color.stIdle} />}>
          <View style={styles.titleRow}>
            <View>
              <Text style={styles.title}>Today</Text>
              {view ? <Text style={styles.label}>{view.label}</Text> : null}
            </View>
            {count && count.total ? (
              <Glass tone="blue" style={styles.tally}>
                <Text style={styles.tallyText}>{count.done} of {count.total} done</Text>
              </Glass>
            ) : null}
          </View>

          <ViewSwitch mode="Day" onChange={onWeek} />
          {!cfg ? <Text style={styles.dim}>Pair with JARVIS in Settings first.</Text> : null}
          {error ? <Text style={styles.error}>{error}</Text> : null}
          {stale ? <Text style={styles.dim}>{stale}</Text> : null}
          {note && (note.uid === null || !shownUids.has(note.uid)) ? (
            <Pressable accessibilityRole="button" accessibilityHint="Dismiss" onPress={() => setNote(null)}>
              <Text style={note.error ? styles.error : styles.said}>{note.text}</Text>
            </Pressable>
          ) : null}

          {view && view.blocks.length ? <DayBar blocks={view.blocks} /> : null}
          {view?.week.length ? <WeekStrip week={view.week} today={view.date} /> : null}

          {view?.yesterday.blocks.length ? (
            <Glass tone="amber" style={styles.yesterday} testID="yesterday">
              <Text style={styles.cardTitle}>Still open from {view.yesterday.label}</Text>
              <Text style={styles.dim}>What became of these? This closes at 04:00.</Text>
              {view.yesterday.blocks.map(card)}
            </Glass>
          ) : null}

          {view ? (
            <View style={styles.meta}>
              {checkinLine(view.checkin, view.blocks) ? <Text style={styles.dim}>{checkinLine(view.checkin, view.blocks)}</Text> : null}
              {view.plan_error ? <Text style={styles.error}>{view.plan_error}</Text> : null}
              {view.events_note ? <Text style={styles.dim}>{view.events_note}</Text> : null}
              {day?.allDay.map((t) => <Text key={t} style={styles.allDay}>All day: {t}</Text>)}
            </View>
          ) : null}

          {view && !view.blocks.length && !view.plan_error ? (
            <Text style={styles.dim}>No plan for today. Ask JARVIS to plan your week.</Text>
          ) : null}

          {day?.rows.map((r, i) => (r.type === 'block' ? card(r.block) : <Line key={`${r.type}-${i}`} row={r} />))}
        </ScrollView>
      </TabSafeArea>
    </View>
  );
}

function Symbol({ name, tint, fallback }: { name: SymbolViewProps['name']; tint: string; fallback: string }) {
  return (
    <SymbolView name={name} size={15} weight="semibold" tintColor={tint}
      fallback={<Text style={[styles.symbolFallback, { color: tint }]}>{fallback}</Text>} />
  );
}

/** A calendar event, timed check, reminder or the now line: one quiet row in the timeline. */
function Line({ row }: { row: Exclude<Row, { type: 'block' }> }) {
  if (row.type === 'now') {
    return (
      <View style={styles.nowRow} testID="now-line" accessibilityLabel={`Now, ${clock(row.at)}`}>
        <View style={styles.nowDot} />
        <View style={styles.nowRule} />
        <Text style={styles.nowText}>{`now ${clock(row.at)}`}</Text>
      </View>
    );
  }
  const [symbol, fallback, text, sub] = row.type === 'event'
    ? ['calendar', '◷', row.title, `${clock(row.at)}–${clock(row.end)}${row.where ? ` · ${row.where}` : ''}`] as const
    : row.type === 'task'
      ? ['clock.arrow.2.circlepath', '↻', row.text, `${clock(row.at)} · timed check${row.skill ? ` · ${row.skill}` : ''}`] as const
      : ['bell', '•', row.text, `${clock(row.at)} · reminder`] as const;
  return (
    <View style={styles.lineRow}>
      <View style={styles.lineIcon}><Symbol name={symbol} tint={color.tMid} fallback={fallback} /></View>
      <View style={styles.grow}>
        <Text style={styles.lineText} numberOfLines={2}>{text}</Text>
        <Text style={styles.lineSub}>{sub}</Text>
      </View>
    </View>
  );
}

function BlockCard(p: {
  block: TodayBlock; now: number; busy: boolean; disabled: boolean; expanded: boolean; onToggle: () => void;
  note: Note | null; onTap: (o: Choice) => void; onDecide: (approve: boolean) => void;
}) {
  const b = p.block;
  const live = phase(b, p.now) === 'now';
  const told = b.outcome?.outcome;
  const sub = [
    told ? null : when(b, p.now),
    b.status === 'moved' ? 'you moved it in your calendar' : null,
    b.status === 'failed' ? 'not in your calendar' : null,
    told && b.outcome?.reason ? b.outcome.reason : null,
  ].filter(Boolean).join(' · ');
  return (
    <Glass tone={live ? 'blue' : 'clear'} style={[styles.block, live && styles.blockLive]} testID={`block-${b.uid}`}>
      <View style={[styles.stripe, { backgroundColor: kindColor(b.kind) }]} />
      <View style={styles.blockBody}>
        <View style={styles.blockHead}>
          <View style={styles.times}>
            <Text style={styles.time}>{clock(b.start)}</Text>
            <Text style={styles.timeEnd}>{clock(b.end)}</Text>
          </View>
          <View style={styles.grow}>
            <Text style={styles.blockTitle}>{b.title}</Text>
            {sub ? <Text style={styles.lineSub} numberOfLines={2}>{sub}</Text> : null}
          </View>
          {told ? (
            <Pressable accessibilityRole="button" accessibilityLabel={`${b.title}: ${OUTCOME_LABEL[told]}. Change`}
              onPress={p.onToggle} disabled={p.disabled} hitSlop={6}>
              <View style={[styles.badge, { borderColor: OUTCOME_COLOR[told] }]}>
                <Text style={[styles.badgeText, { color: OUTCOME_COLOR[told] }]}>{OUTCOME_LABEL[told]}</Text>
              </View>
            </Pressable>
          ) : null}
        </View>

        {p.expanded ? <Choices block={b} now={p.now} told={told} busy={p.busy} disabled={p.disabled} onTap={p.onTap} /> : null}

        {b.offer ? (
          <View style={styles.offer} testID={`offer-${b.uid}`}>
            <Text style={styles.offerText}>{b.offer.label}?{b.offer.why ? ` (${b.offer.why})` : ''}</Text>
            <View style={styles.chips}>
              <Pressable accessibilityRole="button" accessibilityLabel={`Approve: ${b.offer.label}`}
                onPress={() => p.onDecide(true)} disabled={p.disabled} style={({ pressed }) => pressed && styles.pressed}>
                <Glass tone="accent" interactive style={styles.chip}><Text style={styles.approveText}>Approve</Text></Glass>
              </Pressable>
              <Pressable accessibilityRole="button" accessibilityLabel={`Deny: ${b.offer.label}`}
                onPress={() => p.onDecide(false)} disabled={p.disabled} style={({ pressed }) => pressed && styles.pressed}>
                <Glass interactive style={styles.chip}><Text style={styles.chipText}>Deny</Text></Glass>
              </Pressable>
            </View>
          </View>
        ) : null}

        {p.note ? <Text style={p.note.error ? styles.error : styles.said}>{p.note.text}</Text> : null}
      </View>
    </Glass>
  );
}

/** What became of a block: one row of segments once it has started; before that, one "Skip it". */
function Choices(p: {
  block: TodayBlock; now: number; told: Outcome | undefined; busy: boolean; disabled: boolean; onTap: (o: Choice) => void;
}) {
  const options = choices(p.block, p.now);
  const off = p.disabled && !p.busy;
  if (options.length === 1) {
    return (
      <View style={styles.chips}>
        <Pressable accessibilityRole="button" accessibilityLabel={`${p.block.title}: Skip it`}
          accessibilityState={{ disabled: p.disabled }} onPress={() => p.onTap('skipped')} disabled={p.disabled}
          style={({ pressed }) => [pressed && styles.pressed, off && styles.off]}>
          <Glass interactive style={styles.chip}>
            <View style={[styles.chipDot, { backgroundColor: OUTCOME_COLOR.skipped }]} />
            <Text style={styles.chipText}>Skip it</Text>
          </Glass>
        </Pressable>
        {p.busy ? <Text style={styles.dim}>…</Text> : null}
      </View>
    );
  }
  return (
    <Glass style={[styles.segments, off && styles.off]}>
      {options.map((o, i) => (
        <Pressable key={o} accessibilityRole="button" accessibilityLabel={`${p.block.title}: ${OUTCOME_LABEL[o]}`}
          accessibilityState={{ selected: p.told === o, disabled: p.disabled }}
          onPress={() => p.onTap(o)} disabled={p.disabled}
          style={({ pressed }) => [styles.segment, i > 0 && styles.segmentSep, p.told === o && styles.segmentOn,
            pressed && styles.segmentPressed]}>
          <View style={[styles.chipDot, { backgroundColor: OUTCOME_COLOR[o] }]} />
          <Text style={styles.segmentText} numberOfLines={1}>{p.busy ? '…' : OUTCOME_LABEL[o]}</Text>
        </Pressable>
      ))}
    </Glass>
  );
}

/** The day at a glance: one segment per block, coloured by what became of it. */
function DayBar({ blocks }: { blocks: TodayBlock[] }) {
  return (
    <View style={styles.dayBar} accessibilityLabel="The day's blocks">
      {blocks.map((b) => (
        <View key={b.uid} style={[styles.daySeg, { backgroundColor: OUTCOME_COLOR[b.outcome?.outcome ?? 'open'] }]} />
      ))}
    </View>
  );
}

function WeekStrip({ week, today }: { week: WeekDay[]; today: string }) {
  return (
    <View style={styles.week}>
      {week.map((d) => {
        const isToday = d.date === today;
        const inner = (
          <>
            <Text style={[styles.weekDay, isToday && styles.weekToday]}>{d.day}</Text>
            <Text style={[styles.weekDate, isToday && styles.weekToday]}>{Number(d.date.slice(8))}</Text>
            <View style={styles.dots}>
              {weekDots(d).map((dot, i) => <View key={i} style={[styles.dot, { backgroundColor: OUTCOME_COLOR[dot] }]} />)}
            </View>
          </>
        );
        return isToday
          ? <Glass key={d.date} tone="blue" style={styles.weekCol} testID="week-today">{inner}</Glass>
          : <View key={d.date} style={styles.weekCol}>{inner}</View>;
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.bg },
  fill: { flex: 1 },
  grow: { flex: 1 },
  content: { paddingHorizontal: space.s4, paddingBottom: space.s6, gap: space.s3 },
  titleRow: { flexDirection: 'row', alignItems: 'flex-end', justifyContent: 'space-between' },
  title: { color: color.tHi, fontSize: 32, fontWeight: '700', letterSpacing: 0.3 },
  label: { color: color.tMid, fontSize: font.body, marginTop: 2 },
  tally: { borderRadius: 16, paddingHorizontal: space.s3, height: 32, justifyContent: 'center' },
  tallyText: { color: color.tHi, fontSize: font.label, fontWeight: '600' },
  dim: { color: color.tMid, fontSize: font.label, lineHeight: 19 },
  error: { color: color.err, fontSize: font.label, lineHeight: 19 },
  said: { color: color.stIdle, fontSize: font.label, lineHeight: 19 },
  meta: { gap: 2 },
  allDay: { color: color.tHi, fontSize: font.label },
  dayBar: { flexDirection: 'row', gap: 3, height: 5 },
  daySeg: { flex: 1, borderRadius: 3 },
  week: { flexDirection: 'row', gap: 4 },
  weekCol: { flex: 1, alignItems: 'center', paddingVertical: space.s2, borderRadius: 14, gap: 2 },
  weekDay: { color: color.tDim, fontSize: font.micro, fontWeight: '600', textTransform: 'uppercase' },
  weekDate: { color: color.tMid, fontSize: font.body, fontWeight: '600' },
  weekToday: { color: color.tHi },
  dots: { flexDirection: 'row', flexWrap: 'wrap', justifyContent: 'center', gap: 2, minHeight: 6, paddingHorizontal: 2 },
  dot: { width: 5, height: 5, borderRadius: 2.5 },
  yesterday: { borderRadius: 22, padding: space.s4, gap: space.s3 },
  cardTitle: { color: color.tHi, fontSize: font.read, fontWeight: '600' },
  block: { borderRadius: 20, flexDirection: 'row', overflow: 'hidden' },
  blockLive: { borderColor: 'rgba(47,182,240,0.45)', borderWidth: StyleSheet.hairlineWidth },
  stripe: { width: 4 },
  blockBody: { flex: 1, padding: space.s3, paddingLeft: space.s3, gap: space.s3 },
  blockHead: { flexDirection: 'row', alignItems: 'center', gap: space.s3 },
  times: { width: 46 },
  time: { color: color.tHi, fontSize: font.body, fontWeight: '600', fontVariant: ['tabular-nums'] },
  timeEnd: { color: color.tDim, fontSize: font.label, fontVariant: ['tabular-nums'] },
  blockTitle: { color: color.tHi, fontSize: font.read, fontWeight: '600' },
  badge: { borderWidth: 1, borderRadius: radius.pill, paddingHorizontal: space.s3, height: 28, justifyContent: 'center' },
  badgeText: { fontSize: font.label, fontWeight: '700' },
  chips: { flexDirection: 'row', flexWrap: 'wrap', gap: space.s2, alignItems: 'center' },
  chip: { borderRadius: 17, height: 34, paddingHorizontal: space.s3, flexDirection: 'row', alignItems: 'center', gap: 6 },
  chipDot: { width: 7, height: 7, borderRadius: 3.5 },
  chipText: { color: color.tHi, fontSize: font.label, fontWeight: '600' },
  segments: { borderRadius: 16, flexDirection: 'row', overflow: 'hidden' },
  segment: { flex: 1, height: 46, alignItems: 'center', justifyContent: 'center', gap: 5 },
  segmentSep: { borderLeftColor: color.border, borderLeftWidth: StyleSheet.hairlineWidth },
  segmentOn: { backgroundColor: 'rgba(47,182,240,0.20)' },
  segmentPressed: { backgroundColor: 'rgba(220,230,240,0.08)' },
  segmentText: { color: color.tHi, fontSize: font.label, fontWeight: '600' },
  approveText: { color: color.bg, fontSize: font.label, fontWeight: '700' },
  offer: { gap: space.s2, paddingTop: space.s2, borderTopColor: color.border, borderTopWidth: StyleSheet.hairlineWidth },
  offerText: { color: color.tHi, fontSize: font.body, lineHeight: 21 },
  lineRow: { flexDirection: 'row', alignItems: 'center', gap: space.s3, paddingHorizontal: space.s3 + 4, paddingVertical: 2 },
  lineIcon: { width: 22, alignItems: 'center' },
  lineText: { color: color.tHi, fontSize: font.body },
  lineSub: { color: color.tMid, fontSize: font.label },
  symbolFallback: { fontSize: font.body, fontWeight: '700' },
  nowRow: { flexDirection: 'row', alignItems: 'center', gap: space.s2, paddingVertical: 2 },
  nowDot: { width: 9, height: 9, borderRadius: 4.5, backgroundColor: color.stIdle, shadowColor: color.stIdle, shadowOpacity: 0.9, shadowRadius: 6 },
  nowRule: { flex: 1, height: StyleSheet.hairlineWidth * 2, backgroundColor: 'rgba(47,182,240,0.55)' },
  nowText: { color: color.stIdle, fontSize: font.micro, fontWeight: '700', fontVariant: ['tabular-nums'] },
  off: { opacity: 0.45 },
  pressed: { opacity: 0.75 },
});
