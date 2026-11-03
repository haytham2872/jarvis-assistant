import type { HubView, RecordBand, RecordCall, RecordView } from '../chat/types';
import { nearest, scale, spread } from '../portfolio/chart';

export type Mode = 'corrected' | 'gut';
export const probability = (call: RecordCall, mode: Mode) => mode === 'gut' ? call.raw_p : call.stated_p;
export const pct = (p: number) => `${(p * 100).toFixed(0)}%`;
export const score = (n: number | undefined | null) => n == null ? '—' : n.toFixed(3);
export function heading(view: RecordView) {
  if (!view.available) return 'Record unavailable';
  if (!view.summary.n) return view.open.length || view.void_count || view.withdrawn_count ? 'No score yet' : 'No forecasts yet';
  return view.early ? 'Too early to tell' : 'Track record';
}
export function hubRecord(part: HubView['record']) {
  if (!part) return '';
  return part.early !== false || part.brier === null ? `${part.settled} settled · Too early to tell` : `Brier ${score(part.brier)} · ${part.settled} settled`;
}
export const date = (at: number) => new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', year: 'numeric' }).format(new Date(at * 1000));
/** A stable display-only offset in the outcome rows; never alters probability or scoring. */
export function jitter(id: number) {
  const hash = Math.imul(id ^ 0x45d9f3b, 2654435761) >>> 0;
  return .014 + (hash % 1000) / 1000 * .022;
}
export function monthSeries(view: RecordView, mode: Mode) {
  // Leave unscored months as gaps, not invented zero scores or joined missing periods.
  const rows = view.by_month;
  if (!rows.length) return { days: [] as string[], values: [] as (number | null)[], labels: [] as string[] };
  const map = new Map(rows.map((r) => [r.month, mode === 'gut' ? r.brier_gut : r.brier]));
  const days: string[] = [], values: (number | null)[] = [], labels: string[] = [];
  let d = new Date(`${rows[0].month}-01T12:00Z`);
  const last = rows[rows.length - 1].month;
  while (d.toISOString().slice(0, 7) <= last) {
    const key = d.toISOString().slice(0, 7);
    days.push(`${key}-01`); labels.push(key); values.push(map.get(key) ?? null);
    d.setUTCMonth(d.getUTCMonth() + 1);
  }
  return { days, values, labels };
}
export function geometry(calls: RecordCall[], bands: RecordBand[], mode: Mode, width: number, height: number) {
  const left = 30, right = 55, bottom = 25, top = 12;
  const s = scale([0, 1], { width, height, top, bottom, right }, [0, 1]);
  const x = (p: number) => left + p * (width - left - right);
  const points = calls.map((c) => ({ id: c.id, x: x(probability(c, mode)),
    y: s.y(c.outcome === 1 ? 1 - jitter(c.id) : jitter(c.id)), outcome: c.outcome })).sort((a, b) => a.x - b.x || a.id - b.id);
  const labels = spread(bands.map((b) => s.y(b.hits / b.n) - 7), 16, top, height - bottom - 14);
  return { left, right, bottom, points, diagonal: [x(0), s.y(0), x(1), s.y(1)],
    ticks: [0, .5, 1].map((v) => ({ value: v, x: x(v), y: s.y(v) })),
    bands: bands.map((b, i) => ({ x: x(b.mean_p), y: s.y(b.hits / b.n), lo: s.y(b.interval[0]), hi: s.y(b.interval[1]),
      labelY: labels[i], label: `${pct(b.mean_p)} · ${b.n}` })) };
}
export type Geometry = ReturnType<typeof geometry>;
export function pick(points: Geometry['points'], x: number, y: number) {
  if (!points.length) return null;
  const i = nearest(points.map((p) => p.x), x);
  let best: number | null = null, distance = 18 * 18;
  for (let j = i; j >= 0 && Math.abs(points[j].x - x) <= 18; j--) {
    const d = (points[j].x - x) ** 2 + (points[j].y - y) ** 2;
    if (d <= distance) { best = points[j].id; distance = d; }
  }
  for (let j = i + 1; j < points.length && Math.abs(points[j].x - x) <= 18; j++) {
    const d = (points[j].x - x) ** 2 + (points[j].y - y) ** 2;
    if (d < distance) { best = points[j].id; distance = d; }
  }
  return best;
}
