import type { Notice } from '../chat/types';
import { dayTitle, groups, isLong, merge, newestId, oldestId, preview, replyParams, tone, usedFilters } from './model';
import { color } from '../theme/tokens';

const NOW = new Date(2026, 9, 17, 12, 0);               // Saturday 17 October, noon
const at = (d: number, h: number) => new Date(2026, 9, d, h, 0).getTime() / 1000;
const n = (id: number, kind: string, ts: number, text = `message ${id}`): Notice =>
  ({ id, kind, ts, text, discord: null, read: false });

it('groups by day, newest first, under readable day titles', () => {
  const list = [n(1, 'reminder', at(16, 9)), n(3, 'brief', at(17, 8)), n(2, 'research', at(16, 22))];
  const g = groups(list, 'all', NOW);
  expect(g.map((x) => x.title)).toEqual(['Today', 'Yesterday']);
  expect(g[1].items.map((x) => x.id)).toEqual([2, 1]);
  expect(dayTitle(at(13, 9), NOW)).toBe('Tue 13 Oct');
});

it('a filter keeps its kinds, and only filters with something in them are offered', () => {
  const list = [n(1, 'reminder', at(17, 9)), n(2, 'watch', at(17, 10)), n(3, 'portfolio', at(17, 11))];
  expect(groups(list, 'alerts', NOW)[0].items.map((x) => x.id)).toEqual([3, 2]);
  expect(groups(list, 'goals', NOW)).toEqual([]);
  expect(usedFilters(list).map((f) => f.value)).toEqual(['all', 'reminders', 'alerts']);
});

it('long messages fold, short ones do not', () => {
  expect(isLong(n(1, 'brief', 0, 'x'.repeat(500)))).toBe(true);
  expect(isLong(n(1, 'brief', 0, Array(10).fill('line').join('\n')))).toBe(true);
  expect(isLong(n(1, 'reminder', 0, 'Reminder: gym'))).toBe(false);
});

it('Ask about this names the message and previews it on one line', () => {
  const m = n(12, 'brief', at(17, 8), 'Good morning.\n\nTwo things  today.');
  expect(replyParams(m)).toEqual({ reply: '12', replyLabel: expect.stringMatching(/^Morning brief · /), replyText: 'Good morning. Two things today.' });
  expect(preview({ text: 'y'.repeat(200) }, 10)).toBe(`${'y'.repeat(9)}…`);
});

it('a fresh page replaces what was shown for the same id, and ids bound the paging', () => {
  const shown = [{ ...n(2, 'brief', 1), read: false }, n(1, 'reminder', 0)];
  const merged = merge(shown, [{ ...n(2, 'brief', 1), read: true }, n(3, 'watch', 2)]);
  expect(merged.map((x) => [x.id, x.read])).toEqual([[3, false], [2, true], [1, false]]);
  expect(newestId(merged)).toBe(3);
  expect(oldestId(merged)).toBe(1);
  expect(oldestId([])).toBe(0);
});

it('each kind has its colour', () => {
  expect(tone('reminder')).toBe(color.stListening);
  expect(tone('research')).toBe(color.stThinking);
  expect(tone('watch')).toBe(color.ok);
  expect(tone('brief')).toBe(color.stIdle);
  expect(tone('system')).toBe(color.tMid);
});

it('the preview drops Markdown marks', () => {
  expect(preview({ text: '**Overnight**: the [Camus](https://x) pass.\n- one `code`\n## Head' }))
    .toBe('Overnight: the Camus pass. one code Head');
});
