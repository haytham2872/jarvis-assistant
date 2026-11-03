// The Today tab's logic (slice 2), kept apart from the screen so it can be tested on its own:
// what can be said about a block now, the day as one timeline with a "now" line, the check-in
// line, and the week's dots. Times are Unix seconds, shown in the phone's time (Paris).
import type { Outcome, TodayBlock, TodayView, WeekDay } from '../chat/types';

export type Choice = Exclude<Outcome, 'unknown'>;

export const OUTCOME_LABEL: Record<Outcome, string> = {
  done: 'Done', partly: 'Partly', skipped: 'Skipped', moved: 'Other time', unknown: 'Not answered',
};

/** Once a block has started he can say anything about it; before that, only that it won't happen
 * (the server offers it a new slot). The same rule the server applies. */
export function choices(b: TodayBlock, now: number): Choice[] {
  return b.start <= now ? ['done', 'partly', 'skipped', 'moved'] : ['skipped'];
}

export function clock(ts: number): string {
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

export function localDate(ts: number): string {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function span(seconds: number): string {
  const m = Math.max(1, Math.round(seconds / 60));
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  return m % 60 ? `${h} h ${m % 60} min` : `${h} h`;
}

export type Phase = 'ahead' | 'now' | 'past';

export function phase(b: { start: number; end: number }, now: number): Phase {
  return now < b.start ? 'ahead' : now < b.end ? 'now' : 'past';
}

/** "in 25 min", "ends in 40 min", or "" once it is over. */
export function when(b: { start: number; end: number }, now: number): string {
  const p = phase(b, now);
  return p === 'ahead' ? `in ${span(b.start - now)}` : p === 'now' ? `ends in ${span(b.end - now)}` : '';
}

export type Row =
  | { type: 'block'; at: number; block: TodayBlock }
  | { type: 'event'; at: number; end: number; title: string; where: string }
  | { type: 'reminder'; at: number; text: string }
  | { type: 'task'; at: number; text: string; skill: string }
  | { type: 'now'; at: number };

const ORDER: Record<Row['type'], number> = { now: 0, block: 1, event: 2, task: 3, reminder: 4 };

/** The day in time order: blocks, his calendar events, timed checks and reminders, with a "now"
 * line where now falls (only while the view is of today). All-day events are kept apart. */
export function timeline(v: TodayView, now: number): { allDay: string[]; rows: Row[] } {
  const rows: Row[] = [
    ...v.blocks.map((block): Row => ({ type: 'block', at: block.start, block })),
    ...(v.events ?? []).filter((e) => !e.all_day)
      .map((e): Row => ({ type: 'event', at: e.start, end: e.end, title: e.title, where: e.where })),
    ...v.tasks.map((t): Row => ({ type: 'task', at: t.at, text: t.text, skill: t.skill ?? '' })),
    ...v.reminders.map((r): Row => ({ type: 'reminder', at: r.at, text: r.text })),
  ];
  if (localDate(now) === v.date) rows.push({ type: 'now', at: now });
  rows.sort((a, b) => a.at - b.at || ORDER[a.type] - ORDER[b.type]);
  return { allDay: (v.events ?? []).filter((e) => e.all_day).map((e) => e.title), rows };
}

export function tally(blocks: TodayBlock[]): { done: number; told: number; total: number } {
  return {
    done: blocks.filter((b) => b.outcome?.outcome === 'done').length,
    told: blocks.filter((b) => b.outcome).length,
    total: blocks.length,
  };
}

/** One line about tonight's check-in: when it comes, that it is waiting (and a tap answers it),
 * or that the taps already covered everything it would have asked. */
export function checkinLine(c: TodayView['checkin'], blocks: TodayBlock[]): string {
  if (!blocks.length) return '';
  if (!c.enabled) return 'The evening check-in is off.';
  const open = blocks.some((b) => !b.outcome);
  if (c.waiting) return open ? 'The evening check-in is waiting. A tap here answers it.' : 'Check-in answered.';
  if (c.sent) return 'Check-in done.';
  return open ? `Evening check-in at ${c.time}.` : `Nothing left for the ${c.time} check-in.`;
}

export type Dot = Outcome | 'open';

/** One dot per planned block: what became of it, then the ones not yet told. */
export function weekDots(d: WeekDay): Dot[] {
  const told: Dot[] = (['done', 'partly', 'moved', 'skipped', 'unknown'] as const)
    .flatMap((o) => Array<Dot>(Math.max(0, d[o] ?? 0)).fill(o));
  return [...told, ...Array<Dot>(Math.max(0, d.planned - told.length)).fill('open')].slice(0, Math.max(d.planned, 0));
}
