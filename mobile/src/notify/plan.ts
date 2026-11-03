// What the phone notifies about by itself (slice 4), as pure decisions the IO module carries out.
//
// Expo Go cannot be relied on for a server's push, so the app schedules notifications itself for
// JARVIS's messages with a known time (backend/mobile/upcoming.py). When a push from the server has
// been confirmed on this phone, the server pushes every message as it is sent, and scheduling the
// same things here would buzz twice: nothing is scheduled then.
import type { NoticeKind, NoticeRoute, PushState, UpcomingItem } from '../chat/types';

export const MAX_SCHEDULED = 60;          // iOS keeps at most 64 pending notifications per app
const MIN_LEAD_S = 5;                     // anything this close is already happening

export const ROUTES: { value: NoticeRoute; label: string; detail: string }[] = [
  { value: 'discord', label: 'Discord', detail: 'As before: a Discord message. The Inbox keeps a copy.' },
  { value: 'both', label: 'Both', detail: 'A Discord message, and this phone is told too.' },
  {
    value: 'phone', label: 'Phone',
    detail: 'No Discord message. Reminders, the brief, scheduled checks and the check-in buzz here; '
      + 'anything without a known time waits in the Inbox unless server notifications are on.',
  },
];

export const KIND_LABEL: Record<NoticeKind, string> = {
  reminder: 'Reminder', brief: 'Morning brief', check: 'Scheduled check', job: 'Background work',
  research: 'Research', watch: 'Watch', forecast: 'Forecast', goal: 'Goal', checkin: 'Evening check-in',
  review: 'Weekly review', plan: 'Your plan', portfolio: 'Portfolio', profile: 'Profile research',
  pause: 'Paused work', system: 'JARVIS', notice: 'JARVIS',
};

export const kindLabel = (kind: string): string => KIND_LABEL[kind as NoticeKind] ?? 'JARVIS';

/** Does this phone get told at all? */
export const phoneIsTold = (route: NoticeRoute): boolean => route !== 'discord';

/** The notifications to schedule now: none when the phone is not told, or when the server's
 * pushes already reach it; otherwise the coming ones, soonest first, within iOS's limit. */
export function toSchedule(items: UpcomingItem[], route: NoticeRoute, push: PushState, nowS: number): UpcomingItem[] {
  if (!phoneIsTold(route) || push === 'confirmed') return [];
  return items
    .filter((i) => i.at > nowS + MIN_LEAD_S)
    .sort((a, b) => a.at - b.at)
    .slice(0, MAX_SCHEDULED);
}

/** Which tab a tapped notification opens. A server push names its message; a scheduled one its screen. */
export function screenFor(data: Record<string, unknown> | undefined | null): '/inbox' | '/today' | null {
  if (!data) return null;                 // the test notification names no screen: it opens nothing
  if (data.screen === 'today' || data.kind === 'checkin') return '/today';
  if (data.screen === 'inbox' || typeof data.notice_id === 'number' || typeof data.kind === 'string') return '/inbox';
  return null;
}

/** What the push state means, in words, for Settings. */
export function pushLine(push: PushState | 'unavailable', reason = ''): string {
  if (push === 'confirmed') return 'On: JARVIS\'s messages arrive here as he sends them.';
  if (push === 'waiting') return 'Registered; waiting for the test notification to show on this phone.';
  if (push === 'unavailable') return `Not available here${reason ? ` (${reason})` : ''}. Scheduled notifications still work.`;
  return 'Not tried yet.';
}
