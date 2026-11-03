import { card, departure, headline, hubOvernight, sinceFor, timeline } from './model';
import type { OvernightItem, OvernightView } from '../chat/types';

const at = Date.parse('2026-10-25T07:20Z') / 1000;
const item = (kind: OvernightItem['kind']): OvernightItem => ({ id: kind, at, kind, title: 'A concrete subject', detail: 'What happened', link: null, ok: null });
const quiet: OvernightView = { as_of: at, since: at - 3600, items: [], notes: [], complete: true };

it.each([
  ['research', 'Research pass'], ['rumination', 'A second thought'], ['watch', 'An alert fired'], ['night_task', 'A night task'],
  ['timed_check', 'A timed check'], ['forecast', 'A forecast settled'], ['goal', 'A milestone finished'], ['skills', 'His procedures'],
  ['job', 'A background job'], ['letter', 'A letter to his future self'],
] as const)('has deliberate wording for %s', (kind, label) => {
  const words = card(item(kind)); expect(words.label).toBe(label); expect(words.title).toBe('A concrete subject'); expect(words.detail).toBe('What happened');
});
it('never calls an unavailable or partially readable night quiet', () => {
  expect(headline(quiet)).toBe('A quiet night.');
  expect(headline(quiet, true)).toBe('Last saved night');
  const partial = { ...quiet, complete: false, notes: ['Research passes could not be read just now.'] };
  expect(headline(partial)).toBe('The night could not be fully read');
  expect(headline({ ...partial, items: [item('watch')] })).toBe('Part of the night');
  expect(hubOvernight({ since: quiet.since, count: 0, complete: false })).toContain('Some sources unavailable');
});
it('explains a forecast lean rather than pretending one result scores a probability', () => {
  expect(card({ ...item('forecast'), ok: false }).verdict).toBe('Missed the lean');
  expect(card({ ...item('forecast'), ok: true }).verdict).toBe('Matched the lean');
  expect(card(item('forecast')).verdict).toBe('No directional lean');
  expect(card({ ...item('goal'), event: 'question' }).label).toBe('A question for you');
  expect(card({ ...item('goal'), event: 'delivered' }).label).toBe('A result delivered');
  expect(card({ ...item('job'), ok: false }).verdict).toContain('not finish');
});
it('lets the server own the calendar default and anchors the other periods', () => {
  expect(sinceFor('evening', at - 500, at)).toBeNull();
  expect(sinceFor('day', null, at)).toBe(at - 86400);
  expect(sinceFor('looked', at - 500, at)).toBe(at - 500);
  expect(sinceFor('looked', null, at)).toBeNull();
});
it('keeps the two Paris 02:00 hours apart and sorts newest last without mutating the input', () => {
  const first = { ...item('research'), at: Date.parse('2026-10-25T00:15Z') / 1000 };
  const second = { ...item('watch'), at: Date.parse('2026-10-25T01:15Z') / 1000 };
  const original = [second, first]; const rows = timeline(original);
  expect(rows.map((r) => r.id)).toEqual(['research', 'watch']); expect(original[0]).toBe(second);
  expect(rows[0].hour).toContain('GMT+2'); expect(rows[1].hour).toContain('GMT+1');
  expect(card({ ...first, date_only: true }).when).toContain('Time not recorded');
});
it('records only a meaningful complete visit, through the last fetched snapshot, on departure', () => {
  expect(departure(at - 2, at, quiet, false)).toBeNull();
  expect(departure(at - 10, at + 1, quiet, true)).toBeNull();
  expect(departure(at - 10, at + 1, { ...quiet, complete: false }, false)).toBeNull();
  expect(departure(at + 1, at + 10, quiet, false)).toBeNull(); // only a stale cached snapshot
  expect(departure(at - 10, at + 1, quiet, false)).toBe(at); // no unseen arrivals dismissed
  expect(departure(at + 1, at + 10, quiet, false, at + 2)).toBe(at + 2); // a fresh 304 validated old cached content
});
