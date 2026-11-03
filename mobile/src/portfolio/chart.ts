// The portfolio charts' geometry and words (next plan, slice 3), apart from the drawing: scales,
// the day under a finger, what he held on a day, labels that don't overlap, the gain/loss band,
// and the proposal a dragged line makes. Prices arrive split-adjusted, with trades and levels in
// the same (today's) shares, so nothing here converts shares.
import type { ChartForecast, ChartRange, ChartTrade, Position } from '../chat/types';
import { money } from './model';

export const RANGES: ChartRange[] = ['1M', '6M', '1Y', 'ALL'];

export type Frame = { width: number; height: number; top: number; bottom: number; right: number };

export type Scale = {
  xs: number[];
  lo: number;
  hi: number;
  y: (value: number) => number;
  value: (y: number) => number;
};

const num = (s: string | null | undefined): number | null => {
  if (s === null || s === undefined || s === '') return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
};

export function numbers(values: (string | null)[]): (number | null)[] {
  return values.map(num);
}

/** x for every point, spread over the width; y from a range that also holds `extra` (levels,
 * the average cost) so their lines stay on the chart, with a little room above and below. */
export function scale(values: (number | null)[], f: Frame, extra: number[] = []): Scale {
  const all = [...values.filter((v): v is number => v !== null), ...extra.filter(Number.isFinite)];
  let lo = all.length ? Math.min(...all) : 0;
  let hi = all.length ? Math.max(...all) : 1;
  if (hi === lo) {
    const pad = Math.abs(hi) * 0.01 || 1;
    lo -= pad;
    hi += pad;
  }
  const room = (hi - lo) * 0.06;
  lo -= room;
  hi += room;
  const usable = f.width - f.right;
  const n = values.length;
  const xs = values.map((_, i) => (n <= 1 ? usable / 2 : (i / (n - 1)) * usable));
  const span = f.height - f.top - f.bottom;
  return {
    xs, lo, hi,
    y: (v) => f.top + (1 - (v - lo) / (hi - lo)) * span,
    value: (y) => lo + (1 - (y - f.top) / span) * (hi - lo),
  };
}

/** The point nearest a finger's x. */
export function nearest(xs: number[], x: number): number {
  if (!xs.length) return -1;
  let lo = 0;
  let hi = xs.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (xs[mid] <= x) lo = mid;
    else hi = mid;
  }
  return Math.abs(xs[lo] - x) <= Math.abs(xs[hi] - x) ? lo : hi;
}

/** The last index on or before `day` (sorted ISO days); -1 before the first. */
export function indexOn(days: string[], day: string): number {
  let lo = 0;
  let hi = days.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (days[mid] <= day) lo = mid + 1;
    else hi = mid;
  }
  return lo - 1;
}

/** Shares held and their cost at the end of `day`, from his trades (average cost on a sale). */
export function heldOn(trades: ChartTrade[], day: string): { shares: number; basis: number } {
  let shares = 0;
  let basis = 0;
  for (const t of trades) {
    if (t.day > day) break;
    const qty = Number(t.shares);
    if (t.side === 'sell') {
      if (shares > 0) {
        basis -= basis * (qty / shares);
        shares -= qty;
      }
    } else {
      shares += qty;
      basis += qty * Number(t.price) + Number(t.fees);
    }
  }
  // selling in parts can leave a float residue (0.9 - 0.6 - 0.3 is 5.6e-17): that is nothing held
  return shares > 1e-9 ? { shares, basis } : { shares: 0, basis: 0 };
}

export type Scrub = { day: string; price: number; shares: number; value: number | null; pnl: number | null };

/** What a finger on one day reads: the close, and if he held shares then, their value and gain. */
export function scrubAt(days: string[], prices: number[], trades: ChartTrade[], i: number): Scrub | null {
  if (i < 0 || i >= days.length) return null;
  const { shares, basis } = heldOn(trades, days[i]);
  const price = prices[i];
  return { day: days[i], price, shares, value: shares ? shares * price : null, pnl: shares ? shares * price - basis : null };
}

/** Label positions pushed apart (at least `gap` px) in the order given, kept inside [top, bottom]. */
export function spread(ys: number[], gap: number, top: number, bottom: number): number[] {
  const order = ys.map((y, i) => [y, i] as const).sort((a, b) => a[0] - b[0]);
  const out = new Array<number>(ys.length);
  let last = -Infinity;
  for (const [y, i] of order) {
    const placed = Math.max(y, last + gap, top);
    out[i] = placed;
    last = placed;
  }
  // pushed past the bottom: walk back up
  let next = Infinity;
  for (const [, i] of [...order].reverse()) {
    out[i] = Math.min(out[i], next - gap, bottom);
    next = out[i];
  }
  return out;
}

export type Quad = [number, number][];

/** The band between value and cost, as quads: green where he is up, red where he is down. A gap
 * (a missing day) breaks the band. */
export function bandQuads(xs: number[], ys: (number | null)[], cs: (number | null)[]): { gain: Quad[]; loss: Quad[] } {
  const gain: Quad[] = [];
  const loss: Quad[] = [];
  for (let i = 0; i + 1 < xs.length; i++) {
    const [y0, y1, c0, c1] = [ys[i], ys[i + 1], cs[i], cs[i + 1]];
    if (y0 === null || y1 === null || c0 === null || c1 === null) continue;
    const quad: Quad = [[xs[i], y0], [xs[i + 1], y1], [xs[i + 1], c1], [xs[i], c0]];
    // screen y grows downward: the value line above the cost line is a gain
    (y0 + y1 <= c0 + c1 ? gain : loss).push(quad);
  }
  return { gain, loss };
}

/** A price as he would say it: cents above 1, four places below. */
export function roundLevel(level: number): number {
  const places = level >= 1 ? 2 : 4;
  return Math.round(level * 10 ** places) / 10 ** places;
}

export type Proposal = { level: number; direction: 'below' | 'above'; kind: 'price_below' | 'price_above'; words: string };

/** What a level dragged to (or typed) asks: below the last close watches for a fall, above it
 * for a rise. Null for a level that is not a price. */
export function proposal(symbol: string, currency: string, level: number, last: number | null): Proposal | null {
  if (!Number.isFinite(level) || level <= 0) return null;
  const rounded = roundLevel(level);
  const direction = last !== null && rounded > last ? 'above' : 'below';
  return {
    level: rounded, direction, kind: direction === 'below' ? 'price_below' : 'price_above',
    words: `${symbol} closing ${direction} ${money(String(rounded), currency)}`,
  };
}

/** A forecast's dot colour: open, right or wrong once settled, or neither. */
export function forecastTone(f: ChartForecast): 'open' | 'right' | 'wrong' | 'other' {
  if (f.state === 'open') return 'open';
  if (f.state === 'resolved') return f.outcome ? 'right' : 'wrong';
  return 'other';
}

/** A range chip's words; a holding's ALL is the time since he first bought it. */
export function rangeLabel(r: ChartRange, holding: boolean): string {
  return r === 'ALL' ? (holding ? 'Since bought' : 'All') : r;
}

/** "Wrong if below $158.50 · 8.4% away". */
export function levelLabel(direction: string, kind: string, level: string, currency: string, distance: string | null): string {
  const lead = direction === 'wrong_if' ? 'Wrong if' : 'Right if';
  const away = distance === null ? '' : Number(distance) <= 0 ? ' · reached' : ` · ${Number(distance).toFixed(1)}% away`;
  return `${lead} ${kind === 'price_below' ? 'below' : 'above'} ${money(level, currency)}${away}`;
}

/** The day's biggest moves among his holdings, largest first. */
export function movers(positions: Position[], n = 3): Position[] {
  return positions.filter((p) => p.day_change_pct !== null && Number(p.day_change_pct) !== 0)
    .sort((a, b) => Math.abs(Number(b.day_change_pct)) - Math.abs(Number(a.day_change_pct))).slice(0, n);
}
