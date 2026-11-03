// The Portfolio tab's logic (slice 3), apart from the screen: how the ledger's exact figures
// are shown, the heights of a small chart, and a thesis condition in words. The ledger's
// numbers arrive as exact decimal strings; they are rounded here only for display.
import type { Condition, Position, Thesis } from '../chat/types';

const SYMBOL: Record<string, string> = { USD: '$', EUR: '€', GBP: '£' };

function grouped(n: number, places: number): string {
  const [whole, frac] = Math.abs(n).toFixed(places).split('.');
  const withCommas = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return frac ? `${withCommas}.${frac}` : withCommas;
}

function num(s: string | null | undefined): number | null {
  if (s === null || s === undefined || s === '') return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

/** "$1,234.56", "€958.56"; signed: "+€76.37", "−$12.30". "—" when the ledger has no value. */
export function money(s: string | null | undefined, currency: string, signed = false): string {
  const n = num(s);
  if (n === null) return '—';
  const sym = SYMBOL[currency] ?? `${currency} `;
  const sign = n < 0 ? '−' : signed && n > 0 ? '+' : '';
  return `${sign}${sym}${grouped(n, 2)}`;
}

/** "+1.07%", "−7.83%"; "—" when unknown. */
export function pct(s: string | null | undefined, signed = true): string {
  const n = num(s);
  if (n === null) return '—';
  const sign = n < 0 ? '−' : signed && n > 0 ? '+' : '';
  return `${sign}${grouped(n, 2)}%`;
}

export function direction(s: string | null | undefined): 'up' | 'down' | 'flat' {
  const n = num(s);
  return n === null || n === 0 ? 'flat' : n > 0 ? 'up' : 'down';
}

/** Heights 0..1 for a small column chart; a flat series sits in the middle. */
export function heights(values: (string | null)[]): number[] {
  const nums = values.map(num).filter((n): n is number => n !== null);
  if (!nums.length) return [];
  const lo = Math.min(...nums);
  const hi = Math.max(...nums);
  return nums.map((n) => (hi === lo ? 0.5 : 0.08 + 0.92 * ((n - lo) / (hi - lo))));
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** "14 Oct" from "2026-10-14". */
export function shortDate(iso: string | null | undefined): string {
  if (!iso) return '';
  const [, m, d] = iso.split('-').map(Number);
  return m && d ? `${d} ${MONTHS[m - 1]}` : iso;
}

export type ConditionLine = { label: string; detail: string; tone: 'ok' | 'near' | 'hit' | 'neutral'; progress: number | null };

const NEAR_PCT = 5;

/** A condition in words: what it says, how far the last close is (price levels), or when it is
 * due (dated ones), and a tone: reached, within 5%, or still clear. */
export function conditionLine(c: Condition, currency: string): ConditionLine {
  const lead = c.direction === 'wrong_if' ? 'Wrong if' : 'Right if';
  if (c.kind === 'price_below' || c.kind === 'price_above') {
    const word = c.kind === 'price_below' ? 'below' : 'above';
    let label = `${lead} it closes ${word} ${money(c.level, currency)}`;
    if (c.effective_level) label += ` (${money(c.effective_level, currency)} after the split)`;
    if (c.state !== 'open') return { label, detail: stateWord(c.state), tone: 'neutral', progress: null };
    const d = num(c.distance_pct);
    if (d === null) return { label, detail: 'No close to compare yet', tone: 'neutral', progress: null };
    if (d <= 0) return { label, detail: 'Reached at the last close', tone: 'hit', progress: 1 };
    return {
      label, detail: `${grouped(d, 1)}% away`, tone: d <= NEAR_PCT ? 'near' : 'ok',
      progress: Math.max(0.04, 1 - Math.min(d, 50) / 50),      // 50% away or more: almost empty
    };
  }
  const label = `${lead} ${c.criterion}`;
  if (c.state !== 'open') return { label, detail: stateWord(c.state), tone: 'neutral', progress: null };
  return { label, detail: c.due_date ? `Checked by ${shortDate(c.due_date)}` : 'Open', tone: 'neutral', progress: null };
}

function stateWord(state: string): string {
  return ({ hit: 'Happened', not_seen: 'Did not happen', not_hit: 'Not reached', unresolved: 'Could not be settled', settled: 'Settled' } as Record<string, string>)[state]
    ?? state.replace(/_/g, ' ');
}

/** The theses that belong under a holding, and the ones about stocks he only watches. */
export function thesesFor(theses: Thesis[], symbol: string): Thesis[] {
  return theses.filter((t) => t.symbol === symbol);
}

/** What to ask JARVIS about a holding, typed into the chat for him to send. */
export function askAbout(p: Pick<Position, 'symbol'>, thesis?: Thesis): string {
  return thesis ? `Is my ${p.symbol} thesis still holding?` : `How is my ${p.symbol} position doing?`;
}

/** P&L as a percentage of what is still held at cost (value minus P&L), for the total. */
export function totalPnlPct(value: string | null | undefined, pnl: string | null | undefined): string | null {
  const v = num(value);
  const g = num(pnl);
  if (v === null || g === null || v - g === 0) return null;
  return ((g / (v - g)) * 100).toFixed(2);
}

const EVENT: Record<string, string> = {
  condition_added: 'Condition added', condition_hit: 'Condition hit', condition_not_seen: 'Did not happen',
  condition_unresolved: 'Could not be settled', condition_not_hit: 'Not reached', reviewed: 'Reviewed',
  review_incomplete: 'Review incomplete', revised: 'Revised', broken: 'Broken', closed: 'Closed', abandoned: 'Abandoned',
};

/** The thesis's latest event in words; none for its forming, which only repeats the thesis. */
export function eventLine(e: Thesis['last_event']): string | null {
  if (!e || e.kind === 'formed') return null;
  return `${EVENT[e.kind] ?? e.kind.replace(/_/g, ' ')}: ${e.reason} (${shortDate(e.date)})`;
}
