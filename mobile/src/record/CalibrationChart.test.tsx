import { fireEvent, render, screen } from '@testing-library/react-native';
import { CalibrationChart } from './CalibrationChart';
import { thin } from './fixtures';
import type { Geometry } from './model';

let mockGeo: Geometry | null = null;
let mockBroken = false;
const mockDraw = jest.fn();
jest.mock('../ui/skia', () => ({ ...jest.requireActual('../ui/skia'), skiaAvailable: () => true }));
jest.mock('./SkiaCalibration', () => ({ SkiaCalibration: ({ geo }: { geo: Geometry }) => {
  mockDraw(); if (mockBroken) throw new Error('Native drawing failed in fixture'); mockGeo = geo;
  const { View } = require('react-native'); return <View testID="native-record-plot" />;
} }));
const layout = { nativeEvent: { layout: { width: 340, height: 220, x: 0, y: 0 } } };
beforeEach(() => { mockGeo = null; mockBroken = false; mockDraw.mockClear(); });

it('does not load a native drawing for the primary no-settled-calls state', async () => {
  await render(<CalibrationChart calls={[]} bands={[]} mode="corrected" onPick={jest.fn()} />);
  expect(screen.getByText(/No settled calls to plot/)).toBeTruthy(); expect(mockDraw).not.toHaveBeenCalled();
});
it('draws bands and makes native dots select the matching saved call', async () => {
  const v = thin(), pick = jest.fn();
  const r = await render(<CalibrationChart calls={v.settled} bands={v.bands} mode="corrected" onPick={pick} />);
  await fireEvent(screen.getByTestId('record-chart'), 'layout', layout);
  expect(screen.getByTestId('native-record-plot')).toBeTruthy();
  const p = mockGeo!.points[0];
  await fireEvent.press(screen.getByTestId('record-calibration'), { nativeEvent: { locationX: p.x, locationY: p.y } });
  expect(pick).toHaveBeenCalledWith(17);
  await r.rerender(<CalibrationChart calls={v.settled} bands={v.bands_gut} mode="gut" onPick={pick} />);
  expect(mockGeo!.points[0].x).toBeGreaterThan(p.x);
});
it('logs a failed native drawing and retains the readable interval table', async () => {
  const warning = jest.spyOn(console, 'warn').mockImplementation(() => undefined);
  const error = jest.spyOn(console, 'error').mockImplementation(() => undefined);
  try {
    mockBroken = true; const v = thin();
    await render(<CalibrationChart calls={v.settled} bands={v.bands} mode="corrected" onPick={jest.fn()} />);
    await fireEvent(screen.getByTestId('record-chart'), 'layout', layout);
    expect(screen.getByTestId('record-band-table')).toBeTruthy();
    expect(warning).toHaveBeenCalledWith('Native drawing failed; showing its fallback.', expect.any(Error));
  } finally { warning.mockRestore(); error.mockRestore(); }
});
