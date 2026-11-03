// Geometry and Paris wall time for the week. No component or native module needed.
import type { WeekBlock, WeekView } from '../chat/types';

export const FIRST = 7 * 60;
export const LAST = 23 * 60;
export const HOUR = 64;
export const HEIGHT = (LAST - FIRST) * HOUR / 60;
// Day and clock in the phone's own zone: the owner's.
const local = new Intl.DateTimeFormat('en-CA', { year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
export function wall(at: number) {
  const p = Object.fromEntries(local.formatToParts(new Date(at * 1000)).map((v) => [v.type, v.value]));
  return { day: `${p.year}-${p.month}-${p.day}`, minute: Number(p.hour) * 60 + Number(p.minute) };
}
export function addDays(day: string, n: number) {
  const d = new Date(`${day}T12:00:00Z`); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10);
}
export function monday(day: string) { return addDays(day, -((new Date(`${day}T12:00Z`).getUTCDay() + 6) % 7)); }
export function minute(text: string) { const [h, m] = text.split(':').map(Number); return h * 60 + m; }
export function clock(m: number) { return `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`; }
export function instant(day: string, m: number) {
  const target = Date.parse(`${day}T${clock(m)}:00Z`) / 1000;
  let at = target;
  for (let i = 0; i < 3; i++) {
    const w = wall(at);
    at += target - Date.parse(`${w.day}T${clock(w.minute)}:00Z`) / 1000;
  }
  return at;
}
export function snap(m: number, duration = 15) {
  'worklet';
  return Math.max(FIRST, Math.min(Math.floor((LAST - duration) / 15) * 15, Math.round(m / 15) * 15));
}
export function page(offset: number, direction: number) { return Math.max(0, Math.min(4, offset + direction * 3)); }
export type Span = { uid: string; start: number; end: number; event: boolean };
export function spans(view: WeekView): Span[] {
  return view.days.flatMap((d) => [...d.blocks.map((b) => ({ ...b, event: false })),
    ...d.events.map((e) => ({ uid: e.id, start: e.start, end: e.end, event: true }))]);
}
export function clashes(uid: string, start: number, end: number, busy: Span[], buffer = 0) {
  'worklet';
  return busy.some((b) => b.uid !== uid && start < b.end + (b.event ? buffer : 0) * 60 && end > b.start - (b.event ? buffer : 0) * 60);
}
export function geometry(start: number, end: number, day: string) {
  const a = wall(start), b = wall(end);
  const from = a.day < day ? FIRST : Math.max(FIRST, a.minute);
  const to = b.day > day ? LAST : Math.min(LAST, b.minute);
  return { top: (from - FIRST) * HOUR / 60, height: Math.max(0, (to - from) * HOUR / 60) };
}
export function optimistic(view: WeekView, block: WeekBlock, day: string, m: number): WeekView {
  const start = instant(day, m), changed = { ...block, start, end: start + block.end - block.start };
  return { ...view, days: view.days.map((d) => ({ ...d,
    blocks: [...d.blocks.filter((b) => b.uid !== block.uid), ...(d.date === day ? [changed] : [])].sort((a, b) => a.start - b.start),
  })) };
}
