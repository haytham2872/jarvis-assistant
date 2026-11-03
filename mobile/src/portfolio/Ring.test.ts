// The allocation ring's arcs (next plan, slice 3).
import { arcsFor } from './Ring';

it('starts at the top, goes clockwise by weight, and leaves a small gap between holdings', () => {
  const arcs = arcsFor([{ weight: 75, color: 'a' }, { weight: 25, color: 'b' }]);
  expect(arcs.map((a) => [a.start, a.sweep, a.color])).toEqual([[-88.75, 267.5, 'a'], [181.25, 87.5, 'b']]);
  expect(arcsFor([{ weight: 0, color: 'a' }])).toEqual([]);
  expect(arcsFor([{ weight: 0.1, color: 'a' }, { weight: 99.9, color: 'b' }])[0].sweep).toBe(0.5);   // still visible
});
