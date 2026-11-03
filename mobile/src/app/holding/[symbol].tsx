// One holding (next plan, slice 3): its price over 1M, 6M, 1Y or since he bought it, with his
// buys and sells on the line, his average cost, his thesis levels and when they are checked,
// and the forecasts made about the stock. A finger slid along the line reads the day: close,
// what he held then and its gain. Holding still and dragging up or down sets a level; letting go
// asks whether to watch for it or add it to a thesis ("Add an alert at a price" does the same
// without a drag). Below: the ledger's exact figures, the theses, the trades, Ask JARVIS.
import { router, useLocalSearchParams } from 'expo-router';
import * as Haptics from 'expo-haptics';
import { useCallback, useMemo, useState } from 'react';
import { Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { getHolding } from '../../chat/client';
import type { ChartRange, HoldingView } from '../../chat/types';
import { useCachedView, viewKey } from '../../core/useCachedView';
import { forecastTone, levelLabel, numbers, RANGES, rangeLabel, scrubAt, type Scrub } from '../../portfolio/chart';
import { LineChart, type Level, type Marker, type Vertical } from '../../portfolio/LineChart';
import { askAbout, direction, money, pct, shortDate } from '../../portfolio/model';
import { explainHolding } from '../../portfolio/explain';
import { Fact, ThesisBlock } from '../../portfolio/parts';
import { ProposalSheet } from '../../portfolio/ProposalSheet';
import { useApp } from '../../state/AppContext';
import { color, font, radius, space } from '../../theme/tokens';
import { Backdrop } from '../../ui/Backdrop';
import { Glass } from '../../ui/Glass';
import { IconButton } from '../../ui/IconButton';

const REFRESH_MS = 300_000;
const TRADE: Record<string, string> = { buy: color.ok, sell: color.err, opening: color.stIdle };
const FORECAST: Record<ReturnType<typeof forecastTone>, string> = { open: color.tMid, right: color.ok, wrong: color.err, other: color.tDim };
const move = (s: string | number | null | undefined) =>
  (direction(s === null || s === undefined ? null : String(s)) === 'up' ? color.ok
    : direction(s === null || s === undefined ? null : String(s)) === 'down' ? color.err : color.tMid);

export default function Holding() {
  const insets = useSafeAreaInsets();
  const { cfg } = useApp();
  const { symbol = '' } = useLocalSearchParams<{ symbol: string }>();
  const [range, setRange] = useState<ChartRange>('1Y');
  const { data: v, error: loadError, stale, reload } = useCachedView<HoldingView>(
    viewKey(cfg, `holding:${symbol}:${range}`), (cache) => getHolding(cfg!, symbol, range, cache), { refreshMs: REFRESH_MS });
  const error = loadError ? explainHolding(loadError) : '';
  const [scrub, setScrub] = useState<Scrub | null>(null);
  const [sheet, setSheet] = useState<{ level: number | null } | null>(null);
  const [said, setSaid] = useState('');

  const days = useMemo(() => v?.history.map((h) => h[0]) ?? [], [v]);
  const prices = useMemo(() => numbers(v?.history.map((h) => h[1]) ?? []), [v]);
  const last = prices.length ? prices[prices.length - 1] : null;
  const first = prices.find((x) => x !== null) ?? null;
  const change = last !== null && first ? ((last - first) / first) * 100 : null;
  const currency = v?.currency ?? 'USD';

  const levels: Level[] = useMemo(() => (v?.levels ?? []).filter((l) => l.state === 'open').map((l) => ({
    value: Number(l.level), color: l.direction === 'wrong_if' ? color.warn : color.ok,
    label: levelLabel(l.direction, l.kind, l.level, currency, l.distance_pct),
  })), [v, currency]);
  const markers: Marker[] = useMemo(() => [
    ...(v?.trades ?? []).map((t) => ({ day: t.day, value: Number(t.price), color: TRADE[t.side] ?? color.tMid })),
    ...(v?.forecasts ?? []).flatMap((f) => {
      const i = days.indexOf(f.made) >= 0 ? days.indexOf(f.made) : days.findIndex((d) => d >= f.made);
      return i >= 0 && prices[i] !== null ? [{ day: days[i], value: prices[i]!, color: FORECAST[forecastTone(f)], ring: true }] : [];
    }),
  ], [v, days, prices]);
  const verticals: Vertical[] = useMemo(() => (v?.dated ?? []).filter((d) => d.state === 'open')
    .map((d) => ({ day: d.day, color: d.direction === 'wrong_if' ? color.warn : color.ok })), [v]);

  const onScrub = useCallback((i: number | null) => {
    if (i === null || !v) { setScrub(null); return; }
    setScrub(scrubAt(days, prices.map((x) => x ?? 0), v.trades, i));
    void Haptics.selectionAsync();
  }, [v, days, prices]);
  const onPropose = useCallback((level: number) => {
    void Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light);
    setSheet({ level });
  }, []);

  const p = v?.position;
  const bought = (v?.trades.length ?? 0) > 0;
  const name = v?.name ?? p?.name ?? '';
  return (
    <View style={styles.screen}>
      <Backdrop />
      <View style={[styles.header, { paddingTop: insets.top + space.s2 }]}>
        <IconButton symbol="chevron.left" label="Back" onPress={() => (router.canGoBack() ? router.back() : router.replace('/portfolio'))} />
        <View style={styles.grow}>
          <Text style={styles.title} accessibilityRole="header">{symbol.toUpperCase()}</Text>
          {name ? <Text style={styles.dim}>{name}</Text> : null}
        </View>
      </View>
      <ScrollView contentContainerStyle={[styles.content, { paddingBottom: insets.bottom + space.s6 }]}>
        {!cfg ? <Text style={styles.dim}>Pair with JARVIS in Settings first.</Text> : null}
        {error ? <Text style={styles.error}>{error}</Text> : null}
        {stale ? <Text style={styles.dim}>{stale}</Text> : null}
        {v?.stale ? <Text style={styles.dim}>The price feed could not be reached; this is the last history fetched.</Text> : null}
        {said ? <Text style={styles.said}>{said}</Text> : null}

        <Glass tone="blue" style={styles.chartCard}>
          <View style={styles.readout} accessibilityLiveRegion="polite">
            {scrub ? (
              <>
                <Text style={styles.big}>{money(String(scrub.price), currency)}</Text>
                <Text style={styles.dim}>
                  {shortDate(scrub.day)}
                  {scrub.value !== null ? ` · held ${money(String(scrub.value), currency)}` : ' · not held'}
                  {scrub.pnl !== null ? <Text style={{ color: move(scrub.pnl) }}> {money(String(scrub.pnl), currency, true)}</Text> : null}
                </Text>
              </>
            ) : (
              <>
                <Text style={styles.big}>{last !== null ? money(String(last), currency) : '—'}</Text>
                <Text style={[styles.dim, change !== null && { color: move(change) }]}>
                  {change !== null ? `${pct(change.toFixed(2))} over ${rangeLabel(range, bought).toLowerCase()}` : ' '}
                </Text>
              </>
            )}
          </View>
          <LineChart days={days} values={prices} currency={currency} height={220} tint={change !== null && change < 0 ? color.err : color.ok}
            averageCost={v?.average_cost ? Number(v.average_cost) : null} levels={levels} markers={markers} verticals={verticals}
            onScrub={onScrub} onPropose={cfg ? onPropose : undefined} testID="price-chart"
            accessibilityLabel={`${symbol} price chart, ${rangeLabel(range, bought)}${last !== null ? `, last ${money(String(last), currency)}` : ''}`} />
          <View style={styles.chips}>
            {RANGES.map((r) => (
              <Pressable key={r} accessibilityRole="button" accessibilityState={{ selected: r === range }}
                accessibilityLabel={`Show ${rangeLabel(r, bought)}`} onPress={() => { setScrub(null); setRange(r); }}
                style={[styles.chip, r === range && styles.chipOn]}>
                <Text style={[styles.chipText, r === range && styles.chipTextOn]}>{rangeLabel(r, bought)}</Text>
              </Pressable>
            ))}
          </View>
          <Text style={styles.hint}>Slide along the line to read a day. Hold, then drag up or down to set a level.</Text>
          {cfg ? (
            <Pressable accessibilityRole="button" onPress={() => setSheet({ level: null })}>
              <Text style={styles.link}>Add an alert at a price</Text>
            </Pressable>
          ) : null}
        </Glass>

        {v?.split ? (
          <Text style={[styles.dim, { color: color.warn }]}>
            A {v.split.ratio} split on {shortDate(v.split.split_date)} is waiting for your yes; the Portfolio tab has its card.
          </Text>
        ) : null}

        {p ? (
          <Glass style={styles.card}>
            <Fact label="Shares" value={p.shares ?? '—'} />
            <Fact label="Average cost" value={money(p.average_cost, currency)} />
            <Fact label="Value" value={money(p.value, currency)} />
            <Fact label="Gain" value={`${money(p.pnl, currency, true)} · ${pct(p.pnl_pct)}`} />
            {currency !== (p.base_currency ?? 'EUR') ? <Fact label={`In ${p.base_currency ?? 'EUR'}`} value={`${money(p.value_base, p.base_currency ?? 'EUR')} (${money(p.pnl_base, p.base_currency ?? 'EUR', true)})`} /> : null}
            <Fact label="Last close" value={p.close ? `${money(p.close, currency)} on ${shortDate(p.close_date)}` : '—'} />
            {p.weight_pct ? <Fact label="Share of total" value={pct(p.weight_pct, false)} /> : null}
            {Number(p.realised) ? <Fact label="Realised" value={money(p.realised, currency, true)} /> : null}
            {p.close_source ? <Fact label="Source" value={p.close_source} /> : null}
          </Glass>
        ) : v?.closed ? (
          <Text style={styles.dim}>Sold: {money(v.closed.realised, currency, true)} realised.</Text>
        ) : v?.watching ? (
          <Text style={styles.dim}>You watch {symbol}; you don't hold it.</Text>
        ) : null}

        {v?.theses.map((t) => <Glass key={t.id} style={styles.card}><ThesisBlock thesis={t} currency={currency} /></Glass>)}

        {v?.forecasts.length ? (
          <Glass style={styles.card}>
            <Text style={styles.section}>Forecasts</Text>
            {v.forecasts.map((f) => (
              <View key={f.id} style={styles.forecast}>
                <View style={[styles.dot, { borderColor: FORECAST[forecastTone(f)] }]} />
                <Text style={styles.small}>
                  {f.claim}{f.p !== null ? ` · ${Math.round(f.p * 100)}%` : ''} · {forecastTone(f) === 'open'
                    ? `due ${shortDate(f.due)}` : forecastTone(f) === 'right' ? 'happened' : forecastTone(f) === 'wrong' ? "didn't happen" : f.state}
                </Text>
              </View>
            ))}
          </Glass>
        ) : null}

        {p?.trades.length ? (
          <Glass style={styles.card}>
            <Text style={styles.section}>Trades</Text>
            {p.trades.map((t) => (
              <Text key={t.id} style={styles.small}>
                {t.side === 'split' ? `Split ×${t.shares}` : `${t.side[0].toUpperCase()}${t.side.slice(1)} ${t.shares} at ${money(t.price, t.currency)}`} · {shortDate(t.traded_on)}
              </Text>
            ))}
          </Glass>
        ) : null}

        {v ? (
          <Pressable accessibilityRole="button" accessibilityLabel={`Ask JARVIS about ${symbol}`}
            onPress={() => router.navigate({ pathname: '/', params: { draft: askAbout({ symbol: v.symbol }, v.theses[0]) } })}
            style={({ pressed }) => pressed && styles.pressed}>
            <Glass interactive tone="blue" style={styles.btn}><Text style={styles.btnText}>Ask JARVIS</Text></Glass>
          </Pressable>
        ) : null}
      </ScrollView>

      {cfg && v ? (
        <ProposalSheet visible={sheet !== null} cfg={cfg} symbol={v.symbol} currency={currency} last={last}
          level={sheet?.level ?? null} theses={v.theses} explain={explainHolding} onClose={() => setSheet(null)}
          onDone={(text) => { setSheet(null); setSaid(text); void reload(true); }} />
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.bg },
  header: { flexDirection: 'row', alignItems: 'center', gap: space.s3, paddingHorizontal: space.s4, paddingBottom: space.s2 },
  grow: { flex: 1 },
  title: { color: color.tHi, fontSize: 26, fontWeight: '700' },
  content: { paddingHorizontal: space.s4, gap: space.s3 },
  dim: { color: color.tMid, fontSize: font.label, lineHeight: 19 },
  small: { color: color.tMid, fontSize: font.label, lineHeight: 18, flexShrink: 1 },
  error: { color: color.err, fontSize: font.label, lineHeight: 19 },
  said: { color: color.stIdle, fontSize: font.label, lineHeight: 19 },
  chartCard: { borderRadius: 24, padding: space.s3, gap: space.s2 },
  readout: { minHeight: 52, justifyContent: 'center' },
  big: { color: color.tHi, fontSize: 30, fontWeight: '700', fontVariant: ['tabular-nums'] },
  chips: { flexDirection: 'row', gap: space.s2, flexWrap: 'wrap' },
  chip: { borderRadius: radius.pill, borderWidth: 1, borderColor: color.border, paddingHorizontal: space.s3, height: 30, justifyContent: 'center' },
  chipOn: { borderColor: color.stIdle, backgroundColor: 'rgba(47,182,240,0.15)' },
  chipText: { color: color.tMid, fontSize: font.label, fontWeight: '600' },
  chipTextOn: { color: color.tHi },
  hint: { color: color.tDim, fontSize: font.micro, lineHeight: 15 },
  link: { color: color.stIdle, fontSize: font.label },
  card: { borderRadius: 20, padding: space.s3, gap: space.s2 },
  section: { color: color.tMid, fontSize: font.micro, fontWeight: '700', letterSpacing: 0.6, textTransform: 'uppercase' },
  forecast: { flexDirection: 'row', alignItems: 'center', gap: space.s2 },
  dot: { width: 10, height: 10, borderRadius: 5, borderWidth: 2 },
  btn: { alignSelf: 'flex-start', borderRadius: 18, height: 36, paddingHorizontal: space.s4, justifyContent: 'center' },
  btnText: { color: color.tHi, fontSize: font.label, fontWeight: '700' },
  pressed: { opacity: 0.75 },
});
