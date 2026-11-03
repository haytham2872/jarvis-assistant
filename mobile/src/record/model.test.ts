import { call, empty, thin } from './fixtures';
import { date, geometry, heading, hubRecord, jitter, monthSeries, pick, probability } from './model';

it('treats empty, open-only and a thin record as normal and uses the server early flag', () => {
  expect(heading(empty())).toBe('No forecasts yet');
  expect(heading(empty({ open: [call({ outcome: null })] }))).toBe('No score yet');
  expect(heading(thin())).toBe('Too early to tell');
  expect(heading({ ...thin(), threshold: 1, early: false })).toBe('Track record');
  expect(heading(empty({ available: false }))).toBe('Record unavailable');
  expect(hubRecord({ settled: 1, brier: .1089, early: true })).toContain('Too early');
  expect(hubRecord({ settled: 33, brier: .183741, early: false })).toContain('0.184');
});
it('keeps probabilities exact and uses stable display-only jitter within the outcome rows', () => {
  expect(probability(call(), 'gut')).toBe(.731); expect(probability(call(), 'corrected')).toBe(.67);
  expect(jitter(17)).toBe(jitter(17)); expect(jitter(18)).not.toBe(jitter(17));
  for (let i = 1; i < 100; i++) { expect(jitter(i)).toBeGreaterThan(0); expect(jitter(i)).toBeLessThan(.04); }
});
it('scales the five bands with honest intervals, separates labels and selects dots near both axes', () => {
  const v = thin(); const sameY = Array.from({ length: 5 }, (_, i) => ({ ...v.bands[0], lo: i / 5, hi: (i + 1) / 5, mean_p: .1 + i / 5 }));
  const g = geometry([call(), call({ id: 19, outcome: 0 })], sameY, 'corrected', 340, 220);
  const ys = g.bands.map((b) => b.labelY).sort((a, b) => a - b);
  for (let i = 1; i < ys.length; i++) expect(ys[i] - ys[i - 1]).toBeGreaterThanOrEqual(16);
  const p = g.points.find((p) => p.id === 17)!;
  expect(pick(g.points, p.x, p.y)).toBe(17); expect(pick(g.points, 0, 110)).toBeNull();
  expect(g.bands[0].hi).toBeLessThan(g.bands[0].lo);
  expect(g.bands[0].lo - g.bands[0].hi).toBeGreaterThan(100); // a single hit still has a very wide 95% interval
  const gut = geometry([call()], v.bands_gut, 'gut', 340, 220);
  expect(gut.points[0].x).toBeGreaterThan(p.x);
});
it('leaves unscored months as gaps and switches the actual monthly scores', () => {
  const v = thin(); v.by_month = [{ month: '2026-08', n: 1, brier: .1089, brier_gut: .072361 },
    { month: '2026-10', n: 2, brier: .211745, brier_gut: .193211 }];
  expect(monthSeries(v, 'corrected').values).toEqual([.1089, null, .211745]);
  expect(monthSeries(v, 'gut').values).toEqual([.072361, null, .193211]);
  expect(date(Date.parse('2026-09-30T22:15Z') / 1000)).toBe('1 Oct 2026');
});
