import type { UpcomingItem } from '../chat/types';
import { MAX_SCHEDULED, phoneIsTold, pushLine, screenFor, toSchedule } from './plan';

const item = (id: string, at: number, extra: Partial<UpcomingItem> = {}): UpcomingItem =>
  ({ id, kind: 'reminder', at, title: 'Reminder', body: id, screen: 'inbox', ...extra });

it('nothing is scheduled while JARVIS messages go to Discord only', () => {
  expect(phoneIsTold('discord')).toBe(false);
  expect(toSchedule([item('a', 2000)], 'discord', 'unknown', 1000)).toEqual([]);
});

it('nothing is scheduled once server pushes are confirmed on this phone: they would buzz twice', () => {
  expect(toSchedule([item('a', 2000)], 'phone', 'confirmed', 1000)).toEqual([]);
  expect(toSchedule([item('a', 2000)], 'both', 'waiting', 1000)).toHaveLength(1);
});

it('the coming ones, soonest first, within the iOS limit, and nothing already happening', () => {
  const items = [item('late', 5000), item('now', 1003), item('soon', 2000),
    ...Array.from({ length: 80 }, (_, k) => item(`r${k}`, 10_000 + k))];
  const out = toSchedule(items, 'phone', 'unknown', 1000);
  expect(out[0].id).toBe('soon');
  expect(out.map((i) => i.id)).not.toContain('now');
  expect(out).toHaveLength(MAX_SCHEDULED);
});

it('a tapped notification opens its screen; the test notification opens nothing', () => {
  expect(screenFor({ screen: 'today', kind: 'checkin' })).toBe('/today');
  expect(screenFor({ kind: 'checkin', notice_id: 3 })).toBe('/today');
  expect(screenFor({ screen: 'inbox', kind: 'brief' })).toBe('/inbox');
  expect(screenFor({ notice_id: 9, kind: 'research' })).toBe('/inbox');
  expect(screenFor({ test: true, token: 'x' })).toBeNull();
  expect(screenFor(undefined)).toBeNull();
  expect(screenFor({ other: 1 })).toBeNull();
});

it('the push state reads in words', () => {
  expect(pushLine('confirmed')).toMatch(/arrive here/);
  expect(pushLine('waiting')).toMatch(/waiting for the test/);
  expect(pushLine('unavailable', 'iPhone only')).toMatch(/Not available here \(iPhone only\)/);
  expect(pushLine('unknown')).toBe('Not tried yet.');
});
