import type { Condition } from '../chat/types';
import { askAbout, conditionLine, direction, eventLine, heights, money, pct, shortDate, totalPnlPct } from './model';

const cond = (over: Partial<Condition> = {}): Condition => ({
  id: 1, kind: 'price_below', direction: 'wrong_if', level: '140', effective_level: null,
  criterion: 'NVDA closes below 140', due_date: '', state: 'open', distance_pct: '15.34', linked: 'watch', ...over,
});

it('shows exact ledger figures rounded for display, with the currency and a real minus sign', () => {
  expect(money('168.26543523661366', 'EUR')).toBe('€168.27');
  expect(money('1946.2113', 'USD')).toBe('$1,946.21');
  expect(money('76.37', 'EUR', true)).toBe('+€76.37');
  expect(money('-12.3', 'USD', true)).toBe('−$12.30');
  expect(money('0', 'USD', true)).toBe('$0.00');
  expect(money('5', 'CHF')).toBe('CHF 5.00');
  expect(money(null, 'USD')).toBe('—');
  expect([pct('1.07'), pct('-7.83'), pct(null), pct('8.72', false)]).toEqual(['+1.07%', '−7.83%', '—', '8.72%']);
  expect([direction('2'), direction('-0.1'), direction('0'), direction(null)]).toEqual(['up', 'down', 'flat', 'flat']);
});

it('scales a series between its lowest and highest close, a flat one in the middle', () => {
  expect(heights(['100', '150', '200'])).toEqual([0.08, 0.54, 1]);
  expect(heights(['5', '5'])).toEqual([0.5, 0.5]);
  expect(heights([null, '3'])).toEqual([0.5]);
  expect(heights([])).toEqual([]);
  expect(shortDate('2026-10-14')).toBe('14 Oct');
});

it('says how far the last close is from a price level, and when it is close or reached', () => {
  expect(conditionLine(cond(), 'USD')).toEqual({ label: 'Wrong if it closes below $140.00', detail: '15.3% away',
    tone: 'ok', progress: expect.closeTo(0.693, 3) });
  expect(conditionLine(cond({ distance_pct: '3.2' }), 'USD').tone).toBe('near');
  expect(conditionLine(cond({ distance_pct: '-3.03', kind: 'price_above', direction: 'right_if', level: '160' }), 'USD'))
    .toEqual({ label: 'Right if it closes above $160.00', detail: 'Reached at the last close', tone: 'hit', progress: 1 });
  expect(conditionLine(cond({ distance_pct: null }), 'USD').detail).toBe('No close to compare yet');
  expect(conditionLine(cond({ distance_pct: '0' }), 'USD')).toMatchObject({ detail: 'Reached at the last close', tone: 'hit' });
  expect(conditionLine(cond({ distance_pct: '80' }), 'USD').progress).toBe(0.04);           // far: a sliver, never empty
  expect(conditionLine(cond({ distance_pct: '25' }), 'USD').progress).toBe(0.5);
  expect(conditionLine(cond({ effective_level: '46.6667' }), 'USD').label).toBe(
    'Wrong if it closes below $140.00 ($46.67 after the split)');
});

it('gives a dated condition its due date, and a settled one its outcome', () => {
  const dated = cond({ kind: 'dated_event', level: null, criterion: 'Take-Two delays GTA VI again', due_date: '2026-11-19',
    distance_pct: null });
  expect(conditionLine(dated, 'USD')).toEqual({ label: 'Wrong if Take-Two delays GTA VI again', detail: 'Checked by 19 Nov',
    tone: 'neutral', progress: null });
  expect(conditionLine({ ...dated, state: 'not_seen' }, 'USD').detail).toBe('Did not happen');
  expect(conditionLine(cond({ state: 'hit' }), 'USD')).toMatchObject({ detail: 'Happened', progress: null });
});

it('drafts the question to ask about a holding', () => {
  expect(askAbout({ symbol: 'NVDA' })).toBe('How is my NVDA position doing?');
  expect(askAbout({ symbol: 'NVDA' }, { id: 1 } as never)).toBe('Is my NVDA thesis still holding?');
});

it("gives the total's P&L as a share of what it cost", () => {
  expect(totalPnlPct('1518', '618')).toBe('68.67');                // 618 / 900
  expect(totalPnlPct('100', '-25')).toBe('-20.00');                // 125 down to 100
  expect([totalPnlPct(null, '1'), totalPnlPct('5', '5')]).toEqual([null, null]);
});

it("names the thesis's latest event, and skips its forming", () => {
  expect(eventLine({ kind: 'condition_added', reason: 'NVDA closes below 140', date: '2026-10-15' }))
    .toBe('Condition added: NVDA closes below 140 (15 Oct)');
  expect(eventLine({ kind: 'review_partial', reason: 'x', date: '2026-10-15' })).toBe('review partial: x (15 Oct)');
  expect(eventLine({ kind: 'formed', reason: 'the thesis itself', date: '2026-10-15' })).toBeNull();
  expect(eventLine(null)).toBeNull();
  expect(conditionLine(cond({ state: 'not_hit' }), 'USD').detail).toBe('Not reached');
});
