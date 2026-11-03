import type { HubView, OvernightItem, OvernightKind, OvernightView } from '../chat/types';
import { describeFailure } from '../core/api';

export type Period = 'evening' | 'day' | 'looked';
export const periods: { id: Period; label: string }[] = [
  { id: 'evening', label: 'Since last evening' }, { id: 'day', label: 'Last 24 h' }, { id: 'looked', label: 'Since I last looked' },
];
// The phone's own zone: the owner's.
const local = {};
export const dateTime = (at: number) => new Date(at * 1000).toLocaleString('en-GB', { ...local, day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
export const time = (at: number) => new Date(at * 1000).toLocaleTimeString('en-GB', { ...local, hour: '2-digit', minute: '2-digit' });
export function sinceFor(period: Period, looked: number | null, now = Date.now() / 1000) {
  if (period === 'day') return now - 86400;
  // The server owns the Paris calendar/DST default and validates stale phone timestamps.
  return period === 'looked' ? looked : null;
}
export function headline(v: OvernightView, cachedAfterError = false) {
  if (cachedAfterError) return 'Last saved night';
  if (!v.complete) return v.items.length ? 'Part of the night' : 'The night could not be fully read';
  return v.items.length ? 'While you were away' : 'A quiet night.';
}
export function count(n: number) { return `${n} ${n === 1 ? 'thing' : 'things'}`; }
export function hubOvernight(v: NonNullable<HubView['overnight']>) {
  return v.complete ? v.count ? `${count(v.count)} since ${dateTime(v.since)}` : 'A quiet night.' : `${count(v.count)} found · Some sources unavailable`;
}
const labels: Record<OvernightKind, string> = {
  research: 'Research pass', rumination: 'A second thought', watch: 'An alert fired', night_task: 'A night task',
  timed_check: 'A timed check', forecast: 'A forecast settled', goal: "A goal's step", skills: 'His procedures', job: 'A background job', letter: 'A letter to his future self',
};
export function card(item: OvernightItem) {
  const verdict = item.kind === 'forecast' ? item.ok === null ? 'No directional lean' : item.ok ? 'Matched the lean' : 'Missed the lean'
    : item.ok === false ? 'Did not finish successfully' : '';
  const label = item.kind === 'goal' ? item.event === 'question' ? 'A question for you' : item.event === 'delivered' ? 'A result delivered' : 'A milestone finished' : labels[item.kind];
  return { label, title: item.title, detail: item.detail, verdict,
    when: item.date_only ? `${new Date(item.at * 1000).toLocaleDateString('en-GB', { ...local, day: 'numeric', month: 'short' })} · Time not recorded` : dateTime(item.at) };
}
export function timeline(items: OvernightItem[]) {
  let previous = '';
  return [...items].sort((a, b) => a.at - b.at || a.id.localeCompare(b.id)).map((item) => {
    if (item.date_only) return { ...item, hour: '' };
    // Include UTC offset: the two 02:00 hours on the October clock change are different.
    const group = new Date(item.at * 1000).toLocaleString('en-GB', { ...local, day: 'numeric', month: 'short', hour: '2-digit', timeZoneName: 'shortOffset' });
    const heading = group === previous ? '' : group;
    previous = group;
    return { ...item, hour: heading };
  });
}
export function explain(error: unknown, feature: 'overnight' | 'note') {
  const e = error as { status?: number; message?: string };
  if (e?.status === 404) return feature === 'note' && e.message !== 'Not Found' ? e.message! :
    `This server does not have the ${feature === 'note' ? 'note reader' : 'overnight view'} yet. Update it and restart JARVIS.`;
  if (e?.status === 400 || e?.status === 503) return e.message!;
  return describeFailure(e?.status ?? null, e?.message ?? '');
}

/** A brief accidental visit or partial/failed read must not dismiss unseen work. */
export function departure(opened: number, now: number, data: OvernightView | null, failed: boolean, validatedAt = data?.as_of ?? 0) {
  return now - opened >= 3 && data?.complete && !failed && validatedAt >= opened ? Math.min(validatedAt, now) : null;
}
