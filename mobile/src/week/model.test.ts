import { addDays, clashes, clock, FIRST, geometry, instant, monday, page, snap, wall } from './model';

it('uses Paris wall time through the autumn and spring clock changes', () => {
  for (const day of ['2026-10-24', '2026-10-25', '2026-03-28', '2026-03-29']) {
    expect(wall(instant(day, 9 * 60 + 15))).toEqual({ day, minute: 555 });
  }
  expect(instant('2026-10-25', 540) - instant('2026-10-24', 540)).toBe(25 * 3600);
  expect(instant('2026-03-29', 540) - instant('2026-03-28', 540)).toBe(23 * 3600);
  expect(addDays('2026-10-25', 1)).toBe('2026-10-26');
});
it('snaps to quarter-hours and keeps the whole block within 07–23', () => {
  expect(snap(552)).toBe(555);
  expect(snap(546)).toBe(540);
  expect(snap(0)).toBe(FIRST);
  expect(snap(1380, 60)).toBe(1320);
  expect(snap(1380, 67)).toBe(1305);
  expect(clock(555)).toBe('09:15');
});
it('ignores the dragged UID, permits touching blocks and keeps calendar buffers', () => {
  const busy = [{ uid: 'self', start: 100, end: 500, event: false }, { uid: 'meeting', start: 600, end: 900, event: true }];
  expect(clashes('self', 100, 500, busy)).toBe(false);
  expect(clashes('self', 500, 600, busy)).toBe(false);
  expect(clashes('self', 500, 601, busy)).toBe(true);
  expect(clashes('self', 500, 600, busy, 15)).toBe(true);
  expect(clashes('other', 100, 200, busy)).toBe(true);
});
it('covers all seven days with three columns and clips events crossing midnight', () => {
  expect(monday('2026-10-18')).toBe('2026-10-12');
  expect([page(0, 1), page(3, 1), page(4, 1), page(0, -1)]).toEqual([3, 4, 4, 0]);
  expect(geometry(instant('2026-10-19', 6 * 60), instant('2026-10-19', 8 * 60), '2026-10-19')).toEqual({ top: 0, height: 64 });
  expect(geometry(instant('2026-10-18', 22 * 60), instant('2026-10-19', 9 * 60), '2026-10-19')).toEqual({ top: 0, height: 128 });
});
