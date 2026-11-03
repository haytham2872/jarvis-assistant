import { useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import { getRecord } from '../chat/client';
import type { RecordCall, RecordView } from '../chat/types';
import { describeFailure } from '../core/api';
import { useCachedView, viewKey } from '../core/useCachedView';
import { Button, Screen, Sheet } from '../goals/Screen';
import { LineChart } from '../portfolio/LineChart';
import { CalibrationChart } from '../record/CalibrationChart';
import { date, heading, monthSeries, pct, probability, score, type Mode } from '../record/model';
import { useApp } from '../state/AppContext';
import { color, font, space } from '../theme/tokens';
import { Glass } from '../ui/Glass';

export default function RecordScreen() {
  const { cfg } = useApp();
  const key = viewKey(cfg, 'record');
  const { data, error, stale, reload, loaded } = useCachedView<RecordView>(key, (cache) => getRecord(cfg!, cache), { refreshMs: 60_000 });
  const [mode, setMode] = useState<Mode>('corrected');
  const [selected, setSelected] = useState<{ key: string | null; id: number } | null>(null);
  const [limit, setLimit] = useState(20), [discord, setDiscord] = useState<string | null>(null);
  const call = selected?.key === key ? [...(data?.settled ?? []), ...(data?.open ?? [])].find((c) => c.id === selected?.id) : undefined;
  const summary = mode === 'gut' ? data?.summary_gut : data?.summary;
  const months = data ? monthSeries(data, mode) : null;
  const failure = error as { status?: number; message?: string };
  const why = error ? failure.status === 404 ? 'This server does not have the track record yet. Update it and restart JARVIS.'
    : describeFailure(failure.status ?? null, failure.message ?? '') : '';
  const card = (c: RecordCall) => <Pressable key={c.id} accessibilityRole="button" accessibilityLabel={`Open forecast F${c.id}: ${c.claim}`}
    onPress={() => setSelected({ key, id: c.id })} style={({ pressed }) => pressed && { opacity: .7 }}>
    <Glass style={s.card}><View style={s.row}><Text style={s.kicker}>F{c.id} · {c.domain}</Text>
      <Text style={s.number}>{pct(probability(c, mode))}</Text></View>
      <Text style={s.body}>{c.claim}</Text><Text style={s.dim}>{c.outcome === null ? `Due ${date(c.resolve_by)} · ${c.method}` :
        `${c.outcome ? 'Happened' : 'Did not happen'} · ${date(c.resolved_at!)} · ${c.resolver}`}</Text>
    </Glass></Pressable>;
  return <Screen title="Track record" error={why} stale={stale} loading={!!cfg && !data && !loaded} loadingLabel="Loading track record" reload={() => void reload(true)}>
    {!cfg ? <Text style={s.dim}>Pair with JARVIS in Settings first.</Text> : null}
    {data ? <>
      <Glass tone="blue" style={s.card}>
        <Text style={s.kicker}>CONFIDENCE YOU CAN CHECK</Text><Text style={s.title}>{heading(data)}</Text>
        <Text style={s.body}>{data.summary.n ? data.text.split('\n')[0] : data.text}</Text>
        {data.caution ? <Text style={s.caution}>{data.caution}</Text> : null}
        {data.notes.map((n) => <Text key={n} accessibilityRole="alert" style={s.caution}>{n}</Text>)}
      </Glass>
      {data.available ? <>
        <View style={s.row}>{(['corrected', 'gut'] as const).map((m) => <Button key={m} label={m === 'gut' ? 'Gut' : 'Corrected'}
          accent={mode === m} onPress={() => setMode(m)} />)}</View>
        <Text style={s.dim}>{mode === 'gut' ? 'The first probability he gave, before correction.' : 'The probability he actually stated, after checking his record.'}</Text>
        {summary?.n ? <Glass style={s.card}>
          <View style={s.row}><Text style={s.heading}>Brier score</Text><Text style={s.big}>{score(summary.brier)}</Text></View>
          <Text style={s.dim}>0 is perfect. Lower is better.{summary.se == null ? ' One settled call has no standard error yet.' : ` Standard error ± ${score(summary.se)}.`}</Text>
          <Text style={s.body}>{summary.skill == null ? 'No base-rate comparison: all settled outcomes are the same.' :
            `Skill vs always saying the base rate (${pct(summary.base_rate!)}): ${summary.skill >= 0 ? '+' : ''}${summary.skill.toFixed(2)}.`}</Text>
          <Text style={s.dim}>Above 0 means better than that baseline.{data.early ? ' These numbers are mostly noise so far.' : ''}</Text>
        </Glass> : null}
        {data.summary.n ? <><View style={s.section}><Text style={s.heading}>Calibration</Text>
          <CalibrationChart calls={data.settled} bands={mode === 'gut' ? data.bands_gut : data.bands} mode={mode} onPick={(id) => setSelected({ key, id })} /></View>
        <View style={s.section}><Text style={s.heading}>Brier by month</Text>
          {months && data.by_month.length > 1 ? <LineChart days={months.days} values={months.values} currency="" height={150} tint={color.stIdle}
            markers={months.values.flatMap((value, i) => value === null ? [] : [{ day: months.days[i], value, color: color.stIdle }])}
            accessibilityLabel={`Monthly ${mode} Brier scores; lower is better`} testID="record-months" /> : null}
          {!data.by_month.length ? <Text style={s.dim}>Monthly scores appear once a call settles.</Text> : null}
          {data.by_month.map((m) => <View key={m.month} style={s.row}><Text style={s.body}>{m.month} · {m.n} settled</Text>
            <Text style={s.number}>{score(mode === 'gut' ? m.brier_gut : m.brier)}</Text></View>)}
          {data.by_month.length === 1 ? <Text style={s.dim}>One scored month so far; a line needs another month.</Text> : null}
        </View>
        <View style={s.section}><Text style={s.heading}>By area</Text>
          {!data.by_domain.length ? <Text style={s.dim}>No settled calls to group yet.</Text> : null}
          {data.by_domain.map((d) => <View key={d.domain} style={s.row}><Text style={s.body}>{d.domain} · n={d.n}</Text>
            <Text style={s.number}>{score(mode === 'gut' ? d.brier_gut : d.brier)}</Text></View>)}
        </View></> : <Glass style={s.card}><Text style={s.heading}>Waiting for outcomes</Text>
          <Text style={s.dim}>Scores, calibration and monthly history appear once a call settles. Open calls are waiting on evidence; they are never scored as misses.</Text></Glass>}
        <View style={s.section}><Text style={s.heading}>Open calls · {data.open.length}</Text>
          {!data.open.length ? <Text style={s.dim}>No open forecasts.</Text> : null}{data.open.map(card)}
        </View>
        {data.settled.length ? <View style={s.section}><Text style={s.heading}>Settled calls · {data.settled.length}</Text>
          {data.settled.slice(0, limit).map(card)}
          {data.settled.length > limit ? <Button label="Show more settled calls" onPress={() => setLimit((n) => n + 20)} /> : null}
        </View> : null}
        <Text style={s.dim}>{data.void_count} could not be settled · {data.withdrawn_count} withdrawn. These stay in the record and are not scored.</Text>
        <Button label="Read the Discord record" onPress={() => setDiscord(key)} />
      </> : <Button label="Try again" onPress={() => void reload(true)} />}
    </> : null}
    {call && data?.available ? <Sheet title={`Forecast F${call.id}`} onClose={() => setSelected(null)}>
      <Text style={s.heading}>{call.claim}</Text><Text style={s.dim}>{call.domain} · due {date(call.resolve_by)}</Text>
      <Text style={s.body}>True if: {call.criteria}</Text><Text style={s.body}>Gut {pct(call.raw_p)} · Corrected {pct(call.stated_p)}</Text>
      <Text style={s.body}>{call.outcome === null ? `Open · checked via ${call.method}` : `${call.outcome ? 'Happened' : 'Did not happen'} · settled via ${call.resolver} on ${date(call.resolved_at!)}`}</Text>
      {call.resolution_quote ? <><Text style={s.heading}>What settled it</Text><Text selectable style={s.body}>{call.resolution_quote}</Text></> : null}
      <Text style={s.heading}>How it was checked</Text><Text selectable style={s.body}>{call.check_query || 'Only you can settle this in conversation.'}</Text>
      <Text style={s.dim}>The record saves the method, query and quote; it does not save a source URL.</Text>
    </Sheet> : null}
    {discord && discord === key && data ? <Sheet title="The Discord record" onClose={() => setDiscord(null)}><Text selectable style={s.body}>{data.text}</Text></Sheet> : null}
  </Screen>;
}
const s = StyleSheet.create({ card: { padding: space.s4, borderRadius: 20, gap: 10 }, section: { gap: 12 },
  row: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: 12 },
  kicker: { color: color.tMid, fontSize: font.micro, letterSpacing: .7 }, title: { color: color.tHi, fontSize: 28, fontWeight: '700' },
  heading: { color: color.tHi, fontSize: font.read, fontWeight: '600' }, body: { color: color.tHi, fontSize: font.body, lineHeight: 22 },
  dim: { color: color.tMid, fontSize: font.label, lineHeight: 19 }, caution: { color: color.warn, fontSize: font.label, lineHeight: 20 },
  number: { color: color.tHi, fontSize: font.body, fontVariant: ['tabular-nums'] }, big: { color: color.tHi, fontSize: 30, fontWeight: '600', fontVariant: ['tabular-nums'] },
});
