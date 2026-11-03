import type { TodayBlock, TodayView } from '../chat/types';
import { checkinLine, choices, clock, localDate, phase, tally, timeline, weekDots, when } from './model';

// Tuesday 6 October 2026 in the machine's local time, as the app reads times in the phone's.
const at = (h: number, m = 0) => new Date(2026, 9, 6, h, m).getTime() / 1000;

const block = (over: Partial<TodayBlock> = {}): TodayBlock => ({
  uid: 'g', kind: 'gym', title: 'Gym', start: at(17, 30), end: at(18, 30), status: 'applied',
  outcome: null, started: false, offer: null, ...over,
});

const view = (over: Partial<TodayView> = {}): TodayView => ({
  date: localDate(at(12)), label: 'Tuesday 6 Oct', now: at(12),
  blocks: [block({ uid: 'w', kind: 'work', title: 'Work', start: at(9), end: at(17) }), block()],
  yesterday: { date: '2026-10-05', label: 'Monday', blocks: [] },
  checkin: { enabled: true, time: '21:30', waiting: false, sent: false },
  reminders: [{ at: at(19), text: 'call the notary', repeat: 'once' }],
  tasks: [{ at: at(22), text: 'NVDA close', repeat: 'weekdays', skill: '' }],
  week: [], events: [{ title: 'Lunch with Karim', start: at(12, 30), end: at(13, 30), all_day: false, where: '' },
    { title: 'Mum\'s birthday', start: at(0), end: at(23, 59), all_day: true, where: '' }],
  events_note: '', ...over,
});

it('offers every outcome once a block has started, and only skipping before', () => {
  expect(choices(block(), at(17, 29))).toEqual(['skipped']);
  expect(choices(block(), at(17, 30))).toEqual(['done', 'partly', 'skipped', 'moved']);
});

it('says when a block is, in minutes and hours', () => {
  expect(when(block(), at(17, 5))).toBe('in 25 min');
  expect(when(block(), at(15, 30))).toBe('in 2 h');
  expect(when(block(), at(14, 50))).toBe('in 2 h 40 min');
  expect(when(block(), at(17, 50))).toBe('ends in 40 min');
  expect(when(block(), at(18, 30))).toBe('');
  expect([phase(block(), at(17)), phase(block(), at(18)), phase(block(), at(19))]).toEqual(['ahead', 'now', 'past']);
  expect(clock(at(7, 5))).toBe('07:05');
});

it('lays the day out in time order with the now line where now falls, all-day events apart', () => {
  const { allDay, rows } = timeline(view(), at(14));
  expect(allDay).toEqual(["Mum's birthday"]);
  expect(rows.map((r) => (r.type === 'block' ? r.block.title : r.type === 'event' ? r.title : r.type === 'now' ? 'now'
    : r.text))).toEqual(['Work', 'Lunch with Karim', 'now', 'Gym', 'call the notary', 'NVDA close']);
});

it('draws no now line on a view of another day, and copes with an unreadable calendar', () => {
  const { allDay, rows } = timeline(view({ events: null, date: '2026-10-05' }), at(14));
  expect(allDay).toEqual([]);
  expect(rows.some((r) => r.type === 'now')).toBe(false);
  expect(rows.some((r) => r.type === 'event')).toBe(false);
});

it('puts the now line before a block starting at that very minute', () => {
  const { rows } = timeline(view(), at(17, 30));
  expect(rows.findIndex((r) => r.type === 'now')).toBeLessThan(rows.findIndex((r) => r.type === 'block' && r.block.uid === 'g'));
});

it('counts the day', () => {
  expect(tally([block({ outcome: { outcome: 'done', reason: '', actor: 'owner', at: 1 } }),
    block({ outcome: { outcome: 'skipped', reason: '', actor: 'owner', at: 1 } }), block()]))
    .toEqual({ done: 1, told: 2, total: 3 });
});

it('says what the evening check-in will do, given what was already tapped', () => {
  const told = { outcome: 'done' as const, reason: '', actor: 'owner', at: 1 };
  const c = view().checkin;
  expect(checkinLine(c, [block()])).toBe('Evening check-in at 21:30.');
  expect(checkinLine(c, [block({ outcome: told })])).toBe('Nothing left for the 21:30 check-in.');
  expect(checkinLine({ ...c, waiting: true, sent: true }, [block()])).toBe('The evening check-in is waiting. A tap here answers it.');
  expect(checkinLine({ ...c, waiting: true, sent: true }, [block({ outcome: told })])).toBe('Check-in answered.');
  expect(checkinLine({ ...c, sent: true }, [block()])).toBe('Check-in done.');
  expect(checkinLine({ ...c, enabled: false }, [block()])).toBe('The evening check-in is off.');
  expect(checkinLine(c, [])).toBe('');
});

it('gives the week one dot per planned block, told ones first', () => {
  expect(weekDots({ date: '2026-10-06', day: 'Tue', planned: 4, done: 1, partly: 0, skipped: 1, moved: 0, unknown: 1 }))
    .toEqual(['done', 'skipped', 'unknown', 'open']);
  expect(weekDots({ date: '2026-10-11', day: 'Sun', planned: 0, done: 0, partly: 0, skipped: 0, moved: 0, unknown: 0 }))
    .toEqual([]);
});
