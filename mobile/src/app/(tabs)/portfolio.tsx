// Portfolio (slice 3; next plan, slice 3): the ledger on one screen. The total in the base currency with its
// value over 1M, 6M, 1Y or all of it against what it cost, the allocation ring, the day's
// biggest movers, each holding with its exact figures from the last recorded close (a tap opens
// its own screen: app/holding/[symbol].tsx), a split waiting for his yes (Approve or Deny on the
// card, the same request Discord shows), stocks he only watches, and positions he sold.
// Read-only: trades are recorded by telling JARVIS, and "Ask JARVIS" types the question for him.
import { router } from 'expo-router';
import * as Haptics from 'expo-haptics';
import { useState } from 'react';
import { Pressable, RefreshControl, ScrollView, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView as TabSafeArea } from 'react-native-screens/experimental';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { decideApproval, getPortfolio, getPortfolioHistory, HttpError } from '../../chat/client';
import type { ChartRange, PendingSplit, PortfolioView, Position, Thesis, TotalHistory } from '../../chat/types';
import { describeFailure, ExplainedError } from '../../core/api';
import { useCachedView, viewKey } from '../../core/useCachedView';
import { movers, numbers, RANGES, rangeLabel } from '../../portfolio/chart';
import { LineChart } from '../../portfolio/LineChart';
import { conditionLine, direction, money, pct, shortDate, thesesFor, totalPnlPct } from '../../portfolio/model';
import { TONE, ThesisBlock } from '../../portfolio/parts';
import { Ring } from '../../portfolio/Ring';
import { useApp } from '../../state/AppContext';
import { color, font, radius, space } from '../../theme/tokens';
import { Backdrop } from '../../ui/Backdrop';
import { Glass } from '../../ui/Glass';

const REFRESH_MS = 300_000;          // closes arrive once a session; the view changes rarely

const SLICE = ['#2fb6f0', '#46c28a', '#a06cd5', '#ffb347', '#e5604d', '#7fd3f7', '#c9a7ea'];

const move = (s: string | null | undefined) => (direction(s) === 'up' ? color.ok : direction(s) === 'down' ? color.err : color.tMid);

function explain(e: unknown): string {
  if (e instanceof ExplainedError) return e.message;
  if (e instanceof HttpError && e.status === 404) {
    return "This server doesn't have the Portfolio view yet. Update it and restart JARVIS.";
  }
  const err = e as { status?: number; message?: string };
  return describeFailure(typeof err?.status === 'number' ? err.status : null, err?.message ?? '');
}

function ask(text: string) {
  router.navigate({ pathname: '/', params: { draft: text } });
}

const open = (symbol: string) => router.push({ pathname: '/holding/[symbol]', params: { symbol } });

export default function Portfolio() {
  const insets = useSafeAreaInsets();
  const { cfg } = useApp();
  const { data: view, error: loadError, stale, reload: load } = useCachedView<PortfolioView>(
    viewKey(cfg, 'portfolio'), (cache) => getPortfolio(cfg!, cache), { refreshMs: REFRESH_MS });
  const error = loadError ? explain(loadError) : '';
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState('');
  const [refreshing, setRefreshing] = useState(false);

  const decide = async (s: PendingSplit, approve: boolean) => {
    if (!cfg || busy || !s.approval) return;
    setBusy(true);
    try {
      const r = await decideApproval(cfg, s.approval.id, approve);
      setSaid(r.text.split('\n')[0]);             // the confirmation; the ledger listing after it is for JARVIS
      void Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
      await load(true);
    } catch (e) {
      setSaid(explain(e));
    } finally {
      setBusy(false);
    }
  };

  const onRefresh = async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  };

  const v = view;
  const empty = v && !v.error && !v.positions.length && !v.watching.length && !v.closed.length;
  return (
    <View style={styles.screen}>
      <Backdrop />
      <TabSafeArea edges={{ bottom: true }} style={styles.fill}>
        <ScrollView contentInsetAdjustmentBehavior="never"
          contentContainerStyle={[styles.content, { paddingTop: insets.top + space.s3 }]}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={() => void onRefresh()} tintColor={color.stIdle} />}>
          <Text style={styles.title}>Portfolio</Text>

          {!cfg ? <Text style={styles.dim}>Pair with JARVIS in Settings first.</Text> : null}
          {error ? <Text style={styles.error}>{error}</Text> : null}
          {stale ? <Text style={styles.dim}>{stale}</Text> : null}
          {v?.error ? <Text style={styles.error}>{v.error}</Text> : null}
          {said ? <Text style={styles.said}>{said}</Text> : null}

          {v && v.positions.length ? <Hero view={v} /> : null}
          {v && v.positions.length > 1 ? <Ring positions={v.positions} palette={SLICE} /> : null}
          {v && v.positions.length ? <Movers positions={v.positions} /> : null}
          {v?.splits.map((s) => <SplitCard key={`${s.symbol}${s.split_date}`} split={s} busy={busy} onDecide={(a) => void decide(s, a)} />)}

          {v?.positions.map((p, i) => (
            <PositionCard key={p.symbol} position={p} hue={SLICE[i % SLICE.length]} theses={thesesFor(v.theses, p.symbol)} />
          ))}

          {v?.watching.length ? (
            <View style={styles.group}>
              <Text style={styles.groupTitle}>Watching</Text>
              {v.watching.map((w) => <WatchCard key={w.symbol} item={w} theses={thesesFor(v.theses, w.symbol)} />)}
            </View>
          ) : null}

          {v?.closed.length ? (
            <View style={styles.group}>
              <Text style={styles.groupTitle}>Sold</Text>
              {v.closed.map((c) => (
                <Pressable key={c.symbol} accessibilityRole="button" accessibilityLabel={`${c.symbol}, sold, ${money(c.realised, c.currency, true)} realised`}
                  onPress={() => open(c.symbol)} style={styles.closedRow}>
                  <Text style={styles.body}>{c.symbol} <Text style={styles.dim}>{c.name}</Text></Text>
                  <Text style={[styles.body, { color: move(c.realised) }]}>{money(c.realised, c.currency, true)} realised</Text>
                </Pressable>
              ))}
            </View>
          ) : null}

          {empty ? (
            <Text style={styles.dim}>Nothing in the ledger yet. Tell JARVIS what you hold, with the shares and the price.</Text>
          ) : null}
          {v && v.positions.length ? (
            <Text style={styles.footer}>
              Figures from the ledger at each stock's last recorded close; the total converted at the ECB reference rate of each date. Charts use the
              price feed's history, adjusted for splits.
            </Text>
          ) : null}
        </ScrollView>
      </TabSafeArea>
    </View>
  );
}

function Hero({ view }: { view: PortfolioView }) {
  const { cfg } = useApp();
  const [range, setRange] = useState<ChartRange>('6M');
  const { data: h } = useCachedView<TotalHistory>(
    viewKey(cfg, `portfolio-history:${range}`), (cache) => getPortfolioHistory(cfg!, range, cache), { refreshMs: REFRESH_MS });
  const t = view.total;
  const base = view.base_currency ?? 'EUR';
  const values = numbers(h?.points.map((x) => x.value_base) ?? []);
  const cost = numbers(h?.points.map((x) => x.cost_base) ?? []);
  const known = values.filter((x): x is number => x !== null);
  const trend = known.length > 1 ? (known[known.length - 1] >= known[0] ? color.ok : color.err) : color.stIdle;
  const pnlPct = totalPnlPct(t?.value_base, t?.pnl_base);
  const lastFull = view.history.length ? view.history[view.history.length - 1] : null;
  return (
    <Glass tone="blue" style={styles.hero} testID="hero">
      <Text style={styles.heroLabel}>Tracked total{t?.close_date ? ` · close ${shortDate(t.close_date)}` : ''}</Text>
      {t?.complete ? (
        <>
          <Text style={styles.heroValue} accessibilityLabel={`Total ${money(t.value_base, base)}`}>{money(t.value_base, base)}</Text>
          <View style={styles.heroRow}>
            <Text style={[styles.heroPnl, { color: move(t.pnl_base) }]}>
              {money(t.pnl_base, base, true)}{pnlPct ? ` (${pct(pnlPct)})` : ''}
            </Text>
            {view.day?.change_pct ? (
              <View style={[styles.chip, { borderColor: move(view.day.change_pct) }]}>
                <Text style={[styles.chipText, { color: move(view.day.change_pct) }]}>{pct(view.day.change_pct)} on the day</Text>
              </View>
            ) : null}
          </View>
        </>
      ) : (
        <>
          <Text style={styles.body}>{t?.note || 'Total unavailable.'}</Text>
          {/* the last day every holding had a close: still worth knowing while one is held */}
          {lastFull ? <Text style={styles.small}>Last complete total: {money(lastFull.value_base, base)} ({shortDate(lastFull.date)})</Text> : null}
        </>
      )}
      {values.length > 1 ? (
        <LineChart days={h!.points.map((x) => x.day)} values={values} cost={cost} currency={base} height={120} tint={trend}
          testID="history-chart" accessibilityLabel={`Total value over ${rangeLabel(range, false)}, against what it cost`} />
      ) : h?.note ? <Text style={styles.small}>{h.note}</Text> : null}
      <View style={styles.rangeRow}>
        {RANGES.map((r) => (
          <Pressable key={r} accessibilityRole="button" accessibilityState={{ selected: r === range }}
            accessibilityLabel={`Total over ${rangeLabel(r, false)}`} onPress={() => setRange(r)}
            style={[styles.range, r === range && styles.rangeOn]}>
            <Text style={[styles.rangeText, r === range && styles.rangeTextOn]}>{rangeLabel(r, false)}</Text>
          </Pressable>
        ))}
        <Text style={styles.legendNote}>dashed: what it cost</Text>
      </View>
    </Glass>
  );
}

function Movers({ positions }: { positions: Position[] }) {
  const top = movers(positions);
  if (!top.length) return null;
  return (
    <View style={styles.movers} testID="movers">
      <Text style={styles.groupTitle}>Today's movers</Text>
      <View style={styles.moverRow}>
        {top.map((p) => (
          <Pressable key={p.symbol} accessibilityRole="button" accessibilityLabel={`${p.symbol} ${pct(p.day_change_pct)} on the day`}
            onPress={() => open(p.symbol)} style={({ pressed }) => pressed && styles.pressed}>
            <Glass interactive style={styles.mover}>
              <Text style={styles.moverSymbol}>{p.symbol}</Text>
              <Text style={[styles.moverPct, { color: move(p.day_change_pct) }]}>{pct(p.day_change_pct)}</Text>
            </Glass>
          </Pressable>
        ))}
      </View>
    </View>
  );
}

function SplitCard({ split: s, busy, onDecide }: { split: PendingSplit; busy: boolean; onDecide: (approve: boolean) => void }) {
  return (
    <Glass tone="amber" style={styles.split} testID={`split-${s.symbol}`}>
      <Text style={styles.cardTitle}>{s.symbol}: a {s.ratio} split on {shortDate(s.split_date)}</Text>
      <Text style={styles.body}>
        The price feed ({s.source}) reports it and the ledger doesn't have it
        {s.shares_before && s.shares_after ? `: ${s.shares_before} shares would become ${s.shares_after}, same total cost` : ''}.
        Until it is recorded, {s.symbol}'s figures stay at the last close before it.
      </Text>
      {s.approval ? (
        <View style={styles.actions}>
          <Pressable accessibilityRole="button" accessibilityLabel={`Approve the ${s.symbol} split`} disabled={busy}
            onPress={() => onDecide(true)} style={({ pressed }) => pressed && styles.pressed}>
            <Glass tone="accent" interactive style={styles.btn}><Text style={styles.btnTextDark}>Approve</Text></Glass>
          </Pressable>
          <Pressable accessibilityRole="button" accessibilityLabel={`Deny the ${s.symbol} split`} disabled={busy}
            onPress={() => onDecide(false)} style={({ pressed }) => pressed && styles.pressed}>
            <Glass interactive style={styles.btn}><Text style={styles.btnText}>Deny</Text></Glass>
          </Pressable>
        </View>
      ) : <Text style={styles.dim}>JARVIS will ask you to confirm it.</Text>}
      <Pressable accessibilityRole="button" onPress={() => ask(`The ${s.symbol} split the feed reports is wrong, dismiss it.`)}>
        <Text style={styles.link}>Wrong split? Tell JARVIS to dismiss it</Text>
      </Pressable>
    </Glass>
  );
}

function PositionCard(p: { position: Position; hue: string; theses: Thesis[] }) {
  const x = p.position;
  const flagged = p.theses.flatMap((t) => t.conditions).map((c) => conditionLine(c, x.currency))
    .filter((l) => l.tone === 'near' || l.tone === 'hit');
  return (
    <Glass style={styles.card} testID={`position-${x.symbol}`}>
      <Pressable accessibilityRole="button" accessibilityHint="Opens its chart and details"
        accessibilityLabel={`${x.symbol}, ${money(x.value, x.currency)}, ${pct(x.pnl_pct)}`} onPress={() => open(x.symbol)}>
        <View style={styles.row}>
          <View style={[styles.hue, { backgroundColor: p.hue }]} />
          <View style={styles.grow}>
            <Text style={styles.symbol}>{x.symbol} <Text style={styles.name}>{x.name}</Text></Text>
            <Text style={styles.dim}>{x.shares} sh · avg {money(x.average_cost, x.currency)}</Text>
          </View>
          <View style={styles.right}>
            <Text style={styles.value}>{money(x.value, x.currency)}</Text>
            <Text style={[styles.pnl, { color: move(x.pnl) }]}>{money(x.pnl, x.currency, true)} · {pct(x.pnl_pct)}</Text>
          </View>
        </View>
        <View style={styles.row}>
          <View style={styles.grow}>
            <LineChart days={x.closes.map((c) => c[0])} values={numbers(x.closes.map((c) => c[1]))} currency={x.currency}
              height={28} tint={move(x.pnl_pct)} testID={`chart-${x.symbol}`} accessibilityLabel={`${x.symbol} recent closes`} />
          </View>
          <View style={styles.right}>
            {x.held_for_split ? <Text style={[styles.badge, { color: color.warn }]}>Held for a split</Text> : null}
            {x.day_change_pct ? <Text style={[styles.small, { color: move(x.day_change_pct) }]}>{pct(x.day_change_pct)} on the day</Text> : null}
            {x.close_date ? <Text style={styles.small}>close {shortDate(x.close_date)}</Text> : <Text style={styles.small}>no close yet</Text>}
          </View>
        </View>
      </Pressable>
      {/* only what needs his eye on the list: the conditions near or reached */}
      {flagged.map((l, i) => <Text key={i} style={[styles.small, { color: TONE[l.tone] }]}>{l.label}: {l.detail}</Text>)}
    </Glass>
  );
}

function WatchCard({ item, theses }: { item: PortfolioView['watching'][number]; theses: Thesis[] }) {
  return (
    <Glass style={styles.card} testID={`watch-${item.symbol}`}>
      <Pressable accessibilityRole="button" accessibilityHint="Opens its chart"
        accessibilityLabel={`${item.symbol}, watching, ${money(item.close, item.currency)}`} onPress={() => open(item.symbol)}>
        <View style={styles.row}>
          <View style={styles.grow}><Text style={styles.symbol}>{item.symbol} <Text style={styles.name}>{item.name}</Text></Text></View>
          <View style={styles.right}>
            <Text style={styles.value}>{money(item.close, item.currency)}</Text>
            {item.close_date ? <Text style={styles.small}>close {shortDate(item.close_date)}</Text> : null}
          </View>
        </View>
      </Pressable>
      {theses.map((t) => <ThesisBlock key={t.id} thesis={t} currency={item.currency} />)}
    </Glass>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.bg },
  fill: { flex: 1 },
  grow: { flex: 1 },
  content: { paddingHorizontal: space.s4, paddingBottom: space.s6, gap: space.s3 },
  title: { color: color.tHi, fontSize: 32, fontWeight: '700', letterSpacing: 0.3 },
  dim: { color: color.tMid, fontSize: font.label, lineHeight: 19 },
  error: { color: color.err, fontSize: font.label, lineHeight: 19 },
  said: { color: color.stIdle, fontSize: font.label, lineHeight: 19 },
  body: { color: color.tHi, fontSize: font.body, lineHeight: 21 },
  small: { color: color.tMid, fontSize: font.label, lineHeight: 18 },
  footer: { color: color.tDim, fontSize: font.micro, lineHeight: 16, marginTop: space.s2 },
  hero: { borderRadius: 24, padding: space.s4, gap: space.s2 },
  heroLabel: { color: color.tMid, fontSize: font.label, fontWeight: '600', letterSpacing: 0.4, textTransform: 'uppercase' },
  heroValue: { color: color.tHi, fontSize: 36, fontWeight: '700', fontVariant: ['tabular-nums'] },
  heroRow: { flexDirection: 'row', alignItems: 'center', gap: space.s3, flexWrap: 'wrap' },
  heroPnl: { fontSize: font.read, fontWeight: '600', fontVariant: ['tabular-nums'] },
  chip: { borderWidth: 1, borderRadius: radius.pill, paddingHorizontal: space.s3, height: 26, justifyContent: 'center' },
  chipText: { fontSize: font.label, fontWeight: '700' },
  rangeRow: { flexDirection: 'row', alignItems: 'center', gap: space.s2, flexWrap: 'wrap' },
  range: { borderRadius: radius.pill, borderWidth: 1, borderColor: color.border, paddingHorizontal: space.s3, height: 28, justifyContent: 'center' },
  rangeOn: { borderColor: color.stIdle, backgroundColor: 'rgba(47,182,240,0.15)' },
  rangeText: { color: color.tMid, fontSize: font.label, fontWeight: '600' },
  rangeTextOn: { color: color.tHi },
  legendNote: { color: color.tDim, fontSize: font.micro, marginLeft: 'auto' },
  movers: { gap: space.s2 },
  moverRow: { flexDirection: 'row', gap: space.s2 },
  mover: { borderRadius: 16, paddingHorizontal: space.s3, paddingVertical: space.s2, alignItems: 'center', minWidth: 84 },
  moverSymbol: { color: color.tHi, fontSize: font.label, fontWeight: '700' },
  moverPct: { fontSize: font.label, fontWeight: '700', fontVariant: ['tabular-nums'] },
  split: { borderRadius: 22, padding: space.s4, gap: space.s3 },
  cardTitle: { color: color.tHi, fontSize: font.read, fontWeight: '600' },
  actions: { flexDirection: 'row', gap: space.s2 },
  btn: { alignSelf: 'flex-start', borderRadius: 18, height: 36, paddingHorizontal: space.s4, justifyContent: 'center' },
  btnText: { color: color.tHi, fontSize: font.label, fontWeight: '700' },
  btnTextDark: { color: color.bg, fontSize: font.label, fontWeight: '700' },
  link: { color: color.stIdle, fontSize: font.label },
  card: { borderRadius: 20, padding: space.s3, gap: space.s2 },
  row: { flexDirection: 'row', alignItems: 'center', gap: space.s3 },
  hue: { width: 4, alignSelf: 'stretch', borderRadius: 2 },
  symbol: { color: color.tHi, fontSize: font.read, fontWeight: '700' },
  name: { color: color.tMid, fontSize: font.label, fontWeight: '400' },
  right: { alignItems: 'flex-end', gap: 2 },
  value: { color: color.tHi, fontSize: font.read, fontWeight: '600', fontVariant: ['tabular-nums'] },
  pnl: { fontSize: font.label, fontWeight: '600', fontVariant: ['tabular-nums'] },
  badge: { fontSize: font.micro, fontWeight: '700', textTransform: 'uppercase' },
  group: { gap: space.s2, marginTop: space.s2 },
  groupTitle: { color: color.tMid, fontSize: font.label, fontWeight: '700', letterSpacing: 0.6, textTransform: 'uppercase' },
  closedRow: { flexDirection: 'row', justifyContent: 'space-between', paddingHorizontal: space.s2 },
  pressed: { opacity: 0.75 },
});
