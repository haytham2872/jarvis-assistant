import { useFocusEffect } from 'expo-router';
import * as Haptics from 'expo-haptics';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ActivityIndicator, Platform, Pressable, RefreshControl, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { SafeAreaView as TabSafeArea } from 'react-native-screens/experimental';
import { addWeekBlock, applyWeek, getWeek, moveWeekBlock, undoWeek } from '../chat/client';
import { useReduceMotion } from '../chat/Orb';
import type { WeekBlock, WeekChange, WeekView as Data } from '../chat/types';
import { useCachedView, viewKey } from '../core/useCachedView';
import { Button, explainGoal, Sheet } from '../goals/Screen';
import { useApp } from '../state/AppContext';
import { color, font, space } from '../theme/tokens';
import { Backdrop } from '../ui/Backdrop';
import { Glass } from '../ui/Glass';
import { DragBlock, EmptySurface } from './GestureSurface';
import { addDays, clock, FIRST, geometry, HEIGHT, HOUR, instant, minute, monday, optimistic, page, snap, spans, wall } from './model';
import { ViewSwitch } from './ViewSwitch';

const kindColor = (kind: string) => ({ work: color.stIdle, gym: color.ok, reading: color.stThinking })[kind] ?? color.stListening;
const ICON = { done: '✓', partly: '◐', skipped: '×', unknown: '?', moved: '↗' };
type Edit = { block?: WeekBlock; day: string; start: string; kind: string; minutes: string };
function explain(error: unknown) {
  const e = error as { status?: number; message?: string };
  return e?.status === 404 && e?.message === 'Not Found'
    ? 'This server does not have the Week view yet. Update it and restart JARVIS.' : explainGoal(error);
}

export function WeekView({ onDay }: { onDay: () => void }) {
  const { cfg } = useApp();
  const insets = useSafeAreaInsets(), reduced = useReduceMotion();
  const [initial] = useState(() => wall(Date.now() / 1000).day);
  const [week, setWeek] = useState(() => monday(initial));
  const [offset, setOffset] = useState(() => Math.min(4, Math.round((Date.parse(initial) - Date.parse(monday(initial))) / 86400000)));
  const start = addDays(week, offset);
  const key = viewKey(cfg, `week:${start}`);
  const locked = useRef(false), epoch = useRef(0);
  const { data: cachedData, error, stale, reload, setData } = useCachedView<Data>(key,
    (cache) => locked.current && cache.cached ? Promise.resolve(cache.cached) : getWeek(cfg!, start, 3, cache), { refreshMs: 60_000 });
  // Only server-confirmed positions belong in the persistent tab cache.
  const [preview, setPreview] = useState<{ key: string | null; data: Data } | null>(null);
  const data = preview?.key === key ? preview?.data ?? cachedData : cachedData;
  const [now, setNow] = useState(() => Date.now() / 1000);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState('');
  const [undo, setUndo] = useState<number | null>(null);
  const [edit, setEdit] = useState<Edit | null>(null);
  const [width, setWidth] = useState(300);
  useFocusEffect(useCallback(() => {
    if (!key) return;
    const t = setInterval(() => setNow(Date.now() / 1000), 60_000);
    return () => { clearInterval(t); epoch.current++; locked.current = false; setBusy(null); setUndo(null); setEdit(null); setPreview(null); };
  }, [key]));
  useEffect(() => {
    if (undo === null) return;
    const t = setTimeout(() => setUndo(null), 6000);
    return () => clearTimeout(t);
  }, [undo]);
  const mutate = async (id: string, call: () => Promise<WeekChange>, before = data, optimisticView?: Data) => {
    if (!cfg || locked.current || !before) return;
    locked.current = true; setBusy(id); setUndo(null); setNote('');
    const version = epoch.current;
    if (optimisticView) setPreview({ key, data: optimisticView });
    void Haptics.selectionAsync();
    try {
      const result = await call();
      if (version !== epoch.current) return;
      setData(result.week); setEdit(null);
      setNote(result.warning || (result.week.plan_id === before.plan_id ? result.text :
        id === 'undo' ? 'Change undone.' : id === 'apply' ? 'Proposal applied.' : 'Block saved.'));
      if (id !== 'undo' && !result.warning && result.week.plan_id !== before.plan_id) setUndo(result.week.plan_id);
      void Haptics.notificationAsync(result.warning ? Haptics.NotificationFeedbackType.Error : Haptics.NotificationFeedbackType.Success);
    } catch (e) {
      if (version !== epoch.current) return;
      setData(before); setNote(explain(e));
      void Haptics.notificationAsync(Haptics.NotificationFeedbackType.Error);
      void reload(true);
    } finally {
      if (version === epoch.current) { setPreview(null); locked.current = false; setBusy(null); }
    }
  };
  const context = { view_start: start, days: 3, plan_id: data?.plan_id ?? null };
  const blocked = !!busy || !!data?.draft;
  const drop = (uid: string, index: number, m: number) => {
    const b = data?.days.flatMap((d) => d.blocks).find((b) => b.uid === uid), day = data?.days[index]?.date;
    if (!cfg || !b || !day || blocked) return;
    void mutate(uid, () => moveWeekBlock(cfg, uid, context, day, clock(m)), data, optimistic(data!, b, day, m));
  };
  const changeDays = (direction: number) => { if (!busy) { setOffset((n) => page(n, direction)); setNote(''); } };
  const openAdd = (day = data?.days[0]?.date ?? start, m = 9 * 60) => {
    const kind = data?.rules.kinds.find((k) => k.addable);
    setEdit({ day, start: clock(snap(m)), kind: kind?.kind ?? '', minutes: String(kind?.minutes ?? 60) });
  };
  const save = () => {
    if (!cfg || !data || !edit) return;
    if (!/^([01]\d|2[0-3]):(00|15|30|45)$/.test(edit.start)) { setNote('Choose a time on a quarter-hour, such as 14:30.'); return; }
    if (edit.block) {
      void mutate(edit.block.uid, () => moveWeekBlock(cfg, edit.block!.uid, context, edit.day, edit.start), data,
        optimistic(data, edit.block, edit.day, minute(edit.start)));
    } else {
      const duration = Number(edit.minutes);
      if (!Number.isInteger(duration) || duration < 15 || duration > 600 || duration % 15) {
        setNote('Choose 15–600 minutes, in quarter-hours.'); return;
      }
      void mutate('add', () => addWeekBlock(cfg, context, edit.day, edit.start, edit.kind, duration));
    }
  };
  const w = width / 3, busySpans = data ? spans(data) : [], today = wall(now);
  const bases = data?.days.map((d) => instant(d.date, FIRST)) ?? [];
  const changeWeek = (n: number) => { setWeek(addDays(week, n * 7)); setOffset(0); setNote(''); };
  return <View style={s.screen}><Backdrop /><TabSafeArea edges={{ bottom: true }} style={s.fill}>
    <ScrollView contentContainerStyle={[s.content, { paddingTop: Platform.OS === 'web' ? 60 : insets.top + space.s3 }]}
      refreshControl={<RefreshControl refreshing={false} onRefresh={() => { if (!busy) void reload(true); }} tintColor={color.stIdle} />}>
      <View style={s.row}><Text style={s.title}>Your week</Text><Text style={s.dim}>PARIS TIME</Text></View>
      <ViewSwitch mode="Week" onChange={onDay} />
      <View style={s.row}><Button label="‹" accessibilityLabel="Previous week" disabled={!!busy} onPress={() => changeWeek(-1)} />
        <View style={s.center}><Text style={s.heading}>{[week, addDays(week, 6)].map((d) => new Date(`${d}T12:00Z`).toLocaleString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' })).join(' – ')}</Text>
          <Text style={s.dim}>{week.slice(0, 4)}</Text></View>
        <Button label="›" accessibilityLabel="Next week" disabled={!!busy} onPress={() => changeWeek(1)} /></View>
      <View style={s.row}><Button label="This week" disabled={!!busy} onPress={() => { setWeek(monday(initial)); setOffset(Math.min(4, Math.round((Date.parse(initial) - Date.parse(monday(initial))) / 86400000))); }} />
        <Button label="+ Block" accessibilityLabel="Add block" disabled={blocked || !data?.plan_id} onPress={() => openAdd()} /></View>
      {!cfg ? <Text style={s.dim}>Pair with JARVIS in Settings first.</Text> : null}
      {error ? <Text accessibilityRole="alert" style={s.error}>{explain(error)}</Text> : null}
      {stale ? <Text style={s.dim}>{stale}</Text> : null}
      {data?.notes.map((n) => <Text key={n} style={s.dim}>{n}</Text>)}
      {data?.draft ? <Glass tone="blue" style={s.proposal}><Text style={s.heading}>{data.week === addDays(monday(today.day), 7) ? 'Proposal for next week' : 'A proposal is waiting'}</Text>
        <Text style={s.dim}>Dashed blocks show the proposal. Apply it before editing this week.</Text>
        <Button label={busy === 'apply' ? 'Applying…' : 'Apply proposal'} disabled={!!busy}
          onPress={() => void mutate('apply', () => applyWeek(cfg!, context, data.week, data.draft!.plan_id))} /></Glass> : null}
      {!data ? <Text style={s.dim}>Loading your week…</Text> : <>
        <View style={s.row}><Button label="‹ Days" disabled={!!busy || offset === 0} onPress={() => changeDays(-1)} />
          <Text style={s.dim}>3 of 7 days</Text><Button label="Days ›" disabled={!!busy || offset === 4} onPress={() => changeDays(1)} /></View>
        <View style={{ flexDirection: 'row', paddingLeft: 34 }}>{data.days.map((d) => <View key={d.date} style={s.dayHead}>
          <Text style={[s.dayLabel, d.date === today.day && { color: color.stIdle }]}>{d.label}</Text>
          {d.events.filter((e) => e.all_day).map((e) => <Text key={e.id} style={s.dim} numberOfLines={2}>Calendar · {e.title}</Text>)}
        </View>)}</View>
        <View style={{ flexDirection: 'row' }}>
          <View style={{ width: 34, height: HEIGHT }}>{Array.from({ length: 17 }, (_, i) => <Text key={i} style={[s.hour, { top: i * HOUR - 6 }]}>{String(i + 7).padStart(2, '0')}</Text>)}</View>
          <View testID="week-grid" onLayout={(e) => { if (e.nativeEvent.layout.width > 0) setWidth(e.nativeEvent.layout.width); }} style={{ flex: 1, height: HEIGHT }}>
            <View style={StyleSheet.absoluteFill}><EmptySurface disabled={blocked || !data.plan_id}
              onPage={changeDays} onAdd={(x, y) => openAdd(data.days[Math.max(0, Math.min(2, Math.floor(x / w)))]?.date, FIRST + y * 60 / HOUR)}>
              <View pointerEvents="none" style={s.grid}>{Array.from({ length: 17 }, (_, i) => <View key={i} style={[s.rule, { top: i * HOUR }]} />)}
                {[1, 2].map((i) => <View key={i} style={[s.column, { left: i * w }]} />)}</View>
            </EmptySurface></View>
            {data.days.flatMap((d, index) => d.events.filter((e) => !e.all_day).map((e) => {
              const g = geometry(e.start, e.end, d.date);
              return g.height ? <View key={`${d.date}-${e.id}`} accessibilityLabel={`Calendar: ${e.title}`} style={[s.calendar, g, { left: index * w + 2, width: w - 4 }]}>
                <Text style={s.calendarLabel}>Calendar</Text><Text style={s.eventTitle} numberOfLines={2}>{e.title}</Text></View> : null;
            }))}
            {data.draft?.blocks.map((b) => {
              const index = data.days.findIndex((d) => d.date === wall(b.start).day);
              const same = data.days.flatMap((d) => d.blocks).find((a) => a.uid === b.uid && a.start === b.start && a.end === b.end);
              const g = geometry(b.start, b.end, wall(b.start).day);
              return index < 0 || same || !g.height ? null : <View pointerEvents="none" key={b.uid} testID={`ghost-${b.uid}`} style={[s.ghost, g, { left: index * w + 2, width: w - 4, borderColor: kindColor(b.kind) }]}>
                <Text style={s.dim}>{b.title}</Text></View>;
            })}
            {data.days.flatMap((d, index) => d.blocks.map((b) => {
              const g = geometry(b.start, b.end, d.date), k = data.rules.kinds.find((k) => k.kind === b.kind);
              return !g.height ? null : <DragBlock key={b.uid} position={[s.blockPosition, g, { left: index * w, width: w }]}
                  uid={b.uid} index={index} minute={wall(b.start).minute} duration={(b.end - b.start) / 60} width={w}
                  bases={bases} busy={busySpans} buffer={b.kind === 'work' ? 0 : data.rules.buffer}
                  lo={minute(k?.start ?? data.rules.hours.start)} hi={minute(k?.end ?? data.rules.hours.end)}
                  disabled={blocked} reduced={reduced} onDrop={drop} onPage={changeDays}>
                  <Pressable testID={`week-block-${b.uid}`} accessibilityRole="button" accessibilityLabel={`Move ${b.title}, ${d.label}, ${clock(wall(b.start).minute)}`}
                    accessibilityHint="Hold then drag to move, or tap to choose a time" disabled={blocked}
                    onPress={() => { setNote(''); setEdit({ block: b, day: d.date, start: clock(wall(b.start).minute), kind: b.kind, minutes: String((b.end - b.start) / 60) }); }}
                    style={[s.block, { borderLeftColor: kindColor(b.kind), backgroundColor: `${kindColor(b.kind)}25` }]}>
                    <Text style={s.blockTitle} numberOfLines={1}>{b.title} {b.outcome ? ICON[b.outcome] : ''}</Text>
                    <Text style={s.blockClock} numberOfLines={1}>{clock(wall(b.start).minute)}</Text>
                    {busy === b.uid ? <ActivityIndicator size="small" color={color.tHi} accessibilityLabel="Saving block" /> : null}
                  </Pressable>
                </DragBlock>;
            }))}
            {data.days.map((d, i) => d.date === today.day && today.minute >= FIRST && today.minute <= 23 * 60
              ? <View pointerEvents="none" key={d.date} testID="week-now" style={[s.now, { left: i * w, width: w, top: (today.minute - FIRST) * HOUR / 60 }]} /> : null)}
          </View>
        </View>
        <View style={{ flexDirection: 'row', paddingLeft: 34 }}>{data.days.map((d) => <View key={d.date} style={s.dayHead}>
          <Text style={s.dim}>{d.counts.done} done · {d.counts.partly} partly</Text><Text style={s.dim}>{d.counts.skipped} skipped / {d.counts.planned} planned</Text>
          {d.events_note ? <Text style={s.dim}>{d.events_note}</Text> : null}</View>)}</View>
        {!data.plan_id ? <Text style={s.dim}>No applied plan. Ask JARVIS to plan this week, then apply its proposal here.</Text> : null}
        <Text style={s.dim}>Hold a block to move it; hold an empty slot to add. Swipe sideways for other days. Calendar events stay fixed.</Text>
      </>}
    </ScrollView>
    {note && !edit ? <Glass tone="blue" style={[s.proposal, s.toast]} testID="week-feedback">
      <Text accessibilityRole="alert" style={s.heading}>{note}</Text>
      <View style={s.row}>{undo !== null ? <Button label="Undo" disabled={!!busy}
        onPress={() => void mutate('undo', () => undoWeek(cfg!, { ...context, plan_id: undo }))} /> : null}
        <Button label="Dismiss" disabled={!!busy} onPress={() => { setNote(''); setUndo(null); }} /></View>
    </Glass> : null}
    {edit ? <Sheet title={edit.block ? `Move ${edit.block.title}` : 'Add a block'} onClose={() => { if (!busy) { setEdit(null); setNote(''); } }}>
      <Text style={s.dim}>Paris time · 15-minute steps</Text>
      <View style={s.wrap}>{Array.from({ length: 7 }, (_, i) => addDays(week, i)).map((d) => <Button key={d} label={d.slice(5)}
        accessibilityLabel={`Choose ${d}`} accent={d === edit.day} disabled={!!busy} onPress={() => setEdit({ ...edit, day: d })} />)}</View>
      <Text style={s.dim}>Start time</Text><TextInput accessibilityLabel="Block start time" value={edit.start}
        editable={!busy} onChangeText={(v) => setEdit({ ...edit, start: v })} placeholder="14:30" placeholderTextColor={color.tDim} style={s.input} />
      {!edit.block ? <><Text style={s.dim}>Kind</Text><View style={s.wrap}>{data?.rules.kinds.filter((k) => k.addable).map((k) => <Button key={k.kind}
        label={k.kind} accent={k.kind === edit.kind} disabled={!!busy} onPress={() => setEdit({ ...edit, kind: k.kind, minutes: String(k.minutes) })} />)}</View>
        {!edit.kind ? <Text style={s.dim}>No named block kinds yet. Ask JARVIS to add one to the weekly rules. Work is filled by its weekly hours.</Text> : null}
        <Text style={s.dim}>Minutes</Text><TextInput accessibilityLabel="Block duration in minutes" keyboardType="number-pad" value={edit.minutes}
          editable={!busy} onChangeText={(v) => setEdit({ ...edit, minutes: v })} style={s.input} /></> : null}
      {note ? <Text accessibilityRole="alert" style={s.error}>{note}</Text> : null}
      <Button label={busy ? 'Saving…' : 'Save block'} accent disabled={!!busy || !edit.kind} onPress={save} />
    </Sheet> : null}
  </TabSafeArea></View>;
}

const s = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.bg }, fill: { flex: 1 },
  content: { paddingHorizontal: space.s4, paddingBottom: space.s6, gap: space.s3 },
  row: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: 8 },
  center: { alignItems: 'center', flex: 1 }, title: { color: color.tHi, fontSize: 32, fontWeight: '700' },
  heading: { color: color.tHi, fontSize: font.body, fontWeight: '600' },
  dim: { color: color.tMid, fontSize: font.micro, lineHeight: 17 },
  error: { color: color.err, fontSize: font.label, lineHeight: 19 }, notice: { color: color.stIdle, fontSize: font.label, lineHeight: 19 },
  proposal: { padding: space.s3, borderRadius: 18, gap: space.s2 },
  toast: { position: 'absolute', left: space.s4, right: space.s4, bottom: space.s3, zIndex: 100 },
  dayHead: { flex: 1, gap: 4, paddingHorizontal: 3 }, dayLabel: { color: color.tHi, fontSize: font.label, fontWeight: '600' },
  grid: { flex: 1 }, rule: { position: 'absolute', left: 0, right: 0, height: 1, backgroundColor: color.surface3 },
  column: { position: 'absolute', top: 0, bottom: 0, width: 1, backgroundColor: color.surface3 },
  hour: { position: 'absolute', color: color.tDim, fontSize: 11, fontVariant: ['tabular-nums'] },
  calendar: { position: 'absolute', borderRadius: 7, borderLeftWidth: 2, borderLeftColor: color.tDim, backgroundColor: '#242f3e', padding: 4, overflow: 'hidden' },
  calendarLabel: { color: color.tMid, fontSize: 9, fontWeight: '600' }, eventTitle: { color: color.tHi, fontSize: 11 },
  blockPosition: { position: 'absolute', zIndex: 2 }, block: { flex: 1, borderRadius: 7, borderLeftWidth: 3, padding: 4, overflow: 'hidden' },
  blockTitle: { color: color.tHi, fontSize: 12, fontWeight: '600' }, blockClock: { color: color.tMid, fontSize: 11, fontVariant: ['tabular-nums'] },
  ghost: { position: 'absolute', borderWidth: 1, borderStyle: 'dashed', borderRadius: 7, opacity: 0.55, padding: 4 },
  now: { position: 'absolute', height: 1.5, backgroundColor: color.stIdle, zIndex: 60 },
  input: { color: color.tHi, fontSize: font.body, borderColor: color.surface3, borderWidth: 1, borderRadius: 14, padding: 12, minHeight: 48 },
  wrap: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
});
