// The Inbox's pure parts (slice 4): which filter a message falls under, how the list is grouped by
// day, the colour of each kind, and what "Ask about this" puts in the chat.
import { chatWhen } from '../chat/when';
import type { Notice } from '../chat/types';
import { color } from '../theme/tokens';
import { kindLabel } from '../notify/plan';

export type Filter = 'all' | 'reminders' | 'briefs' | 'checks' | 'research' | 'alerts' | 'goals' | 'plan';

export const FILTERS: { value: Filter; label: string; kinds: string[] | null }[] = [
  { value: 'all', label: 'All', kinds: null },
  { value: 'reminders', label: 'Reminders', kinds: ['reminder'] },
  { value: 'briefs', label: 'Briefs', kinds: ['brief'] },
  { value: 'checks', label: 'Checks', kinds: ['check', 'job'] },
  { value: 'research', label: 'Research', kinds: ['research', 'profile'] },
  { value: 'alerts', label: 'Alerts', kinds: ['watch', 'forecast', 'portfolio'] },
  { value: 'goals', label: 'Goals', kinds: ['goal'] },
  { value: 'plan', label: 'Plan', kinds: ['checkin', 'review', 'plan'] },
];

export function matches(n: Notice, filter: Filter): boolean {
  const kinds = FILTERS.find((f) => f.value === filter)?.kinds;
  return !kinds || kinds.includes(n.kind);
}

/** The filters worth showing: All, and those that have at least one message. */
export function usedFilters(notices: Notice[]): typeof FILTERS {
  return FILTERS.filter((f) => !f.kinds || notices.some((n) => f.kinds!.includes(n.kind)));
}

export function tone(kind: string): string {
  switch (kind) {
    case 'reminder': case 'checkin': case 'review': case 'plan': return color.stListening;
    case 'research': case 'profile': case 'goal': return color.stThinking;
    case 'watch': case 'forecast': case 'portfolio': return color.ok;
    case 'brief': case 'check': return color.stIdle;
    default: return color.tMid;
  }
}

function dayKey(ts: number): string {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

/** "Today", "Yesterday", "Mon 12 Oct" — the history list's words, for a day heading. */
export function dayTitle(ts: number, now = new Date()): string {
  const label = chatWhen(ts, now);
  return label.includes(':') ? 'Today' : label;
}

export function timeOf(ts: number): string {
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

export type Group = { key: string; title: string; items: Notice[] };

/** Newest first, one group per day, only what the filter keeps. */
export function groups(notices: Notice[], filter: Filter, now = new Date()): Group[] {
  const out: Group[] = [];
  for (const n of [...notices].sort((a, b) => b.id - a.id)) {
    if (!matches(n, filter)) continue;
    const key = dayKey(n.ts);
    const last = out[out.length - 1];
    if (last && last.key === key) last.items.push(n);
    else out.push({ key, title: dayTitle(n.ts, now), items: [n] });
  }
  return out;
}

/** Long messages open folded; the full text is one tap away. */
export const FOLD_CHARS = 420;
export const isLong = (n: Notice): boolean => n.text.length > FOLD_CHARS || n.text.split('\n').length > 8;

/** A one-line preview for the chat's "answering" chip: plain words, no Markdown marks. */
export function preview(n: Pick<Notice, 'text'>, max = 90): string {
  const flat = n.text
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')         // [text](url) -> text
    .replace(/^\s*(?:[-*•·]|\d+\.|#+)\s+/gm, '')   // list marks, numbers, headings
    .replace(/[*_`~]+/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  return flat.length <= max ? flat : `${flat.slice(0, max - 1).trimEnd()}…`;
}

/** What the chat needs to answer a message: its id and how to name it. */
export function replyParams(n: Notice): { reply: string; replyLabel: string; replyText: string } {
  return { reply: String(n.id), replyLabel: `${kindLabel(n.kind)} · ${chatWhen(n.ts)}`, replyText: preview(n) };
}

export function newestId(notices: Notice[]): number {
  return notices.reduce((m, n) => Math.max(m, n.id), 0);
}

export function oldestId(notices: Notice[]): number {
  return notices.length ? notices.reduce((m, n) => Math.min(m, n.id), Infinity) : 0;
}

/** Merge a fresh page into what is shown: one entry per id, newest first. */
export function merge(shown: Notice[], page: Notice[]): Notice[] {
  const byId = new Map<number, Notice>();
  for (const n of [...shown, ...page]) byId.set(n.id, n);
  return [...byId.values()].sort((a, b) => b.id - a.id);
}

/** Today's morning brief, if it has come (the newest one dated today). */
export function todaysBrief(notices: Notice[], now = new Date()): Notice | null {
  const key = dayKey(now.getTime() / 1000);
  return [...notices].sort((a, b) => b.id - a.id).find((n) => n.kind === 'brief' && dayKey(n.ts) === key) ?? null;
}

/** The speech source for a message (backend/mobile/conversation.spoken_answer). */
export const speechSource = (n: Pick<Notice, 'id'>): string => `notice:${n.id}`;
