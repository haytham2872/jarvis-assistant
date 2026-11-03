// The portfolio charts' geometry and words (next plan, slice 3).
import type { ChartForecast, ChartTrade } from '../chat/types';
import {
  bandQuads, forecastTone, heldOn, indexOn, levelLabel, nearest, proposal, rangeLabel, roundLevel, scale, scrubAt, spread,
} from './chart';

const frame = { width: 300, height: 200, top: 10, bottom: 10, right: 20 };

describe('scale', () => {
  it('spreads x over the usable width and maps the range, with room, top to bottom', () => {
    const s = scale([100, 150, 200], frame);
    expect(s.xs).toEqual([0, 140, 280]);
    expect(s.y(s.hi)).toBeCloseTo(10);
    expect(s.y(s.lo)).toBeCloseTo(190);
    expect(s.lo).toBeCloseTo(94);
    expect(s.value(s.y(123))).toBeCloseTo(123);
  });

  it('keeps a level or the average cost on the chart, and survives a flat or single series', () => {
    expect(scale([100, 110], frame, [60]).lo).toBeLessThan(60);
    const flat = scale([5, 5], frame);
    expect(flat.hi).toBeGreaterThan(5);
    expect(flat.lo).toBeLessThan(5);
    expect(scale([7], frame).xs).toEqual([140]);
    expect(scale([null, 7, null], frame).hi).toBeGreaterThan(7);
  });
});

it('finds the point nearest a finger and the day on or before a date', () => {
  expect(nearest([0, 10, 20, 30], 14)).toBe(1);
  expect(nearest([0, 10, 20, 30], 16)).toBe(2);
  expect(nearest([0, 10], -5)).toBe(0);
  expect(nearest([0, 10], 99)).toBe(1);
  expect(nearest([], 3)).toBe(-1);
  const days = ['2026-09-01', '2026-09-02', '2026-09-04'];
  expect(indexOn(days, '2026-09-03')).toBe(1);
  expect(indexOn(days, '2026-09-04')).toBe(2);
  expect(indexOn(days, '2026-08-01')).toBe(-1);
});

const trades: ChartTrade[] = [
  { id: 1, day: '2026-09-01', side: 'buy', shares: '10', price: '100', fees: '5' },
  { id: 2, day: '2026-09-03', side: 'buy', shares: '10', price: '120', fees: '0' },
  { id: 3, day: '2026-09-05', side: 'sell', shares: '5', price: '130', fees: '0' },
];

it('works out what he held, and its cost, at the end of a day', () => {
  expect(heldOn(trades, '2026-08-31')).toEqual({ shares: 0, basis: 0 });
  expect(heldOn(trades, '2026-09-02')).toEqual({ shares: 10, basis: 1005 });
  expect(heldOn(trades, '2026-09-04')).toEqual({ shares: 20, basis: 2205 });
  const after = heldOn(trades, '2026-09-05');
  expect(after.shares).toBe(15);
  expect(after.basis).toBeCloseTo(1653.75);
});

it('a fractional position sold in parts is not "held" afterwards, whatever float residue is left', () => {
  const parts: ChartTrade[] = [
    { id: 1, day: '2026-09-01', side: 'buy', shares: '0.9', price: '100', fees: '0' },
    { id: 2, day: '2026-09-02', side: 'sell', shares: '0.6', price: '110', fees: '0' },
    { id: 3, day: '2026-09-03', side: 'sell', shares: '0.3', price: '120', fees: '0' },
  ];
  expect(heldOn(parts, '2026-09-03')).toEqual({ shares: 0, basis: 0 });      // 0.9 - 0.6 - 0.3 is 5.6e-17 in floats
  expect(scrubAt(['2026-09-03'], [120], parts, 0)).toMatchObject({ value: null, pnl: null });
});

it('reads a scrubbed day: the close, and the value and gain only while he held shares', () => {
  const days = ['2026-08-31', '2026-09-02'];
  expect(scrubAt(days, [90, 110], trades, 0)).toEqual({ day: '2026-08-31', price: 90, shares: 0, value: null, pnl: null });
  expect(scrubAt(days, [90, 110], trades, 1)).toEqual({ day: '2026-09-02', price: 110, shares: 10, value: 1100, pnl: 95 });
  expect(scrubAt(days, [90, 110], trades, 2)).toBeNull();
});

it('spreads labels apart in order and keeps them inside the chart', () => {
  expect(spread([50, 52, 120], 14, 0, 200)).toEqual([50, 64, 120]);
  expect(spread([195, 190], 14, 0, 200)).toEqual([200, 186]);
  expect(spread([-5], 14, 0, 200)).toEqual([0]);
});

it('shades the band green where he is up and red where he is down, broken by a missing day', () => {
  // value above cost on screen means a smaller y
  const { gain, loss } = bandQuads([0, 10, 20, 30], [40, 40, null, 80], [60, 60, 60, 60]);
  expect(gain).toEqual([[[0, 40], [10, 40], [10, 60], [0, 60]]]);
  expect(loss).toEqual([]);
  expect(bandQuads([0, 10], [80, 90], [60, 60]).loss).toHaveLength(1);
});

it('turns a dragged height into a proposal in words, below or above the last close', () => {
  expect(roundLevel(165.456)).toBe(165.46);
  expect(roundLevel(0.123456)).toBe(0.1235);
  expect(proposal('NVDA', 'USD', 158.504, 170)).toEqual({ level: 158.5, direction: 'below', kind: 'price_below', words: 'NVDA closing below $158.50' });
  expect(proposal('AIR.PA', 'EUR', 210, 198)).toMatchObject({ direction: 'above', kind: 'price_above', words: 'AIR.PA closing above €210.00' });
  expect(proposal('NVDA', 'USD', 120, null)?.direction).toBe('below');
  expect(proposal('NVDA', 'USD', -1, 100)).toBeNull();
  expect(proposal('NVDA', 'USD', NaN, 100)).toBeNull();
});

it('colours forecasts and names ranges and levels', () => {
  const f = (state: string, outcome: number | null): ChartForecast =>
    ({ id: 1, claim: 'x', p: 0.6, state, outcome, made: '2026-09-01', due: null, settled: null });
  expect([f('open', null), f('resolved', 1), f('resolved', 0), f('void', null)].map(forecastTone))
    .toEqual(['open', 'right', 'wrong', 'other']);
  expect([rangeLabel('ALL', true), rangeLabel('ALL', false), rangeLabel('6M', true)]).toEqual(['Since bought', 'All', '6M']);
  expect(levelLabel('wrong_if', 'price_below', '158.5', 'USD', '8.43')).toBe('Wrong if below $158.50 · 8.4% away');
  expect(levelLabel('right_if', 'price_above', '250', 'USD', '-1')).toBe('Right if above $250.00 · reached');
  expect(levelLabel('wrong_if', 'price_below', '90', 'USD', null)).toBe('Wrong if below $90.00');
});
