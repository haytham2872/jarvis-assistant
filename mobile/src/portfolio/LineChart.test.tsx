// The chart with Skia "present": what it hands the drawing, the labels it lays over it, and the
// level a dragged line proposes. The drawing itself is faked; the coordinates are real.
import { act, fireEvent, render, screen } from '@testing-library/react-native';

import type { DrawProps } from './SkiaLineChart';

const mockDraw: { last: DrawProps | null } = { last: null };
jest.mock('../ui/skia', () => ({
  skiaAvailable: () => true,
  SkiaBoundary: ({ children }: { children: unknown }) => children,
}));
jest.mock('./SkiaLineChart', () => ({
  SkiaLineChart: (p: DrawProps) => { mockDraw.last = p; return null; },
}));

import { LineChart } from './LineChart';

async function laidOut() {
  await fireEvent(screen.getByTestId('chart'), 'layout', { nativeEvent: { layout: { width: 308, height: 200 } } });
}

it('hands the drawing its coordinates, levels, trades and the average cost, and labels them in text', async () => {
  await render(<LineChart testID="chart" days={['2026-09-01', '2026-09-02', '2026-09-03']} values={[100, 120, 110]} currency="USD"
    height={200} tint="#46c28a" averageCost={105} accessibilityLabel="NVDA"
    levels={[{ value: 90, label: 'Wrong if below $90.00', color: '#ffb347' }]}
    markers={[{ day: '2026-09-02', value: 120, color: 'green' }, { day: '2026-08-01', value: 80, color: 'red' }]}
    verticals={[{ day: '2026-09-03', color: '#ffb347' }, { day: '2026-12-01', color: '#ffb347' }]} />);
  await laidOut();
  const d = mockDraw.last!;
  expect(d.width).toBe(308);
  expect(d.xs).toEqual([0, 150, 300]);
  expect(d.hlines).toHaveLength(2);                    // the average cost and the level
  expect(d.dots).toEqual([{ x: 150, y: expect.any(Number), color: 'green', ring: undefined }]);   // before the chart: left out
  expect(d.vlines).toEqual([{ x: 300, color: '#ffb347' }]);                                        // after it: left out
  expect(screen.getByText('avg $105.00')).toBeTruthy();
  expect(screen.getByText('Wrong if below $90.00')).toBeTruthy();
  expect(d.onRelease).toBeUndefined();                 // no proposals asked for: no drag
});

it('a line dragged and let go proposes the price at that height, shown while dragging', async () => {
  const onPropose = jest.fn();
  await render(<LineChart testID="chart" days={['a', 'b']} values={[100, 200]} currency="USD" height={200} tint="#fff"
    accessibilityLabel="x" onPropose={onPropose} />);
  await laidOut();
  const d = mockDraw.last!;
  // the scale runs from 94 to 206 between y=12 and y=188: the middle is 150
  await act(() => d.onDrag!(100));
  expect(screen.getByText('$150.00')).toBeTruthy();
  await act(() => d.onRelease!(100));
  expect(onPropose).toHaveBeenCalledWith(expect.closeTo(150, 6));
  await act(() => d.onDrag!(null));
  expect(screen.queryByText('$150.00')).toBeNull();
});

it('passes the scrub through, and shades value against cost when a cost line is given', async () => {
  const onScrub = jest.fn();
  await render(<LineChart testID="chart" days={['a', 'b', 'c']} values={[10, 12, 9]} cost={[11, 11, 11]} currency="EUR" height={100}
    tint="#fff" accessibilityLabel="x" onScrub={onScrub} />);
  await laidOut();
  const d = mockDraw.last!;
  expect(d.onScrub).toBe(onScrub);
  expect(d.band!.gain).toHaveLength(1);                // a to b: above the cost
  expect(d.band!.loss).toHaveLength(1);                // b to c: crosses below
});
