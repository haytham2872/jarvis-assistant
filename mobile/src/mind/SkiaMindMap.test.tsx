import { act, fireEvent, render, screen } from '@testing-library/react-native';
import { Gesture } from 'react-native-gesture-handler';
import { SkiaMindMap } from './SkiaMindMap';
import type { MapProps } from './MindMap';

type G = Record<string, jest.Mock>;
const mockGestures: G[] = [], mockDerived: (() => unknown)[] = [], mockDraws: { kind: string; props: Record<string, unknown> }[] = [];
const mockSchedule = jest.fn((fn: (...a: unknown[]) => void, ...a: unknown[]) => fn(...a));
jest.mock('react-native-worklets', () => ({ scheduleOnRN: (...a: unknown[]) => mockSchedule(...a as [never]) }));
jest.mock('react-native-gesture-handler', () => {
  const build = () => {
    const g: G = {};
    for (const k of ['minDistance', 'maxPointers', 'maxDistance', 'maxDuration', 'onStart', 'onUpdate', 'onEnd', 'onFinalize']) g[k] = jest.fn(() => g);
    mockGestures.push(g); return g;
  };
  return { Gesture: { Pan: build, Pinch: build, Tap: build, Race: jest.fn((...g) => g), Simultaneous: jest.fn((...g) => g) },
    GestureDetector: ({ children }: { children: unknown }) => children };
});
jest.mock('react-native-reanimated', () => {
  const R = require('react');
  return { useSharedValue: (value: unknown) => R.useRef({ value, set(v: unknown) { this.value = v; } }).current,
    useDerivedValue: (fn: () => unknown) => { mockDerived.push(fn); return { get value() { return fn(); } }; } };
});
jest.mock('@shopify/react-native-skia', () => {
  const draw = (kind: string) => (props: Record<string, unknown>) => { mockDraws.push({ kind, props }); return props.children ?? null; };
  const path = () => { const p = { ops: [] as unknown[][], moveTo: (...a: unknown[]) => p.ops.push(['M', ...a]), lineTo: (...a: unknown[]) => p.ops.push(['L', ...a]), detach: () => p }; return p; };
  return { Canvas: draw('Canvas'), Circle: draw('Circle'), Group: draw('Group'), Path: draw('Path'), Text: draw('Text'),
    BlurMask: draw('BlurMask'), Skia: { PathBuilder: { Make: path } }, matchFont: () => ({ measureText: (s: string) => ({ width: s.length * 6 }) }) };
});
// The gesture arithmetic below starts from the identity; fitting to the notes is tested in layout.test.ts.
let mockFit = { x: 0, y: 0, scale: 1 };
jest.mock('./layout', () => ({ ...jest.requireActual('./layout'), fit: () => mockFit }));
const point = { id: 'topics/rates.md', x: 100, y: 200, r: 8 };
const props = (): MapProps & { points: typeof point[] } => ({ view: { nodes: [{ id: point.id, folder: 'topics', name: 'rates.md', title: 'Rates', size: 1,
  created: 100, updated: 200, created_source: 'recorded', link: { folder: 'topics', name: 'rates.md' } }], edges: [], total: 1, truncated: false, complete: true, notes: [] },
  points: [point], hidden: [], until: 201, query: '', reduced: false, onPick: jest.fn() });
async function call(g: G, name: string, e: Record<string, unknown> = {}, success = true) { await act(() => g[name].mock.calls[0][0](e, success)); }
beforeEach(() => { mockGestures.length = 0; mockDerived.length = 0; mockDraws.length = 0; mockSchedule.mockClear(); mockFit = { x: 0, y: 0, scale: 1 }; });

it('composes pan and pinch, stays on the UI thread until a successful still tap, then inverse hit-tests', async () => {
  const p = props(); await render(<SkiaMindMap {...p} />);
  await fireEvent(screen.getByTestId('mind-map-surface'), 'layout', { nativeEvent: { layout: { width: 1000, height: 1000 } } });
  const [pan, zoom, tap] = mockGestures;
  expect(Gesture.Simultaneous).toHaveBeenCalledWith(pan, zoom); expect(Gesture.Race).toHaveBeenCalled();
  expect(pan.minDistance).toHaveBeenCalledWith(8); expect(tap.maxDistance).toHaveBeenCalledWith(6);
  await call(pan, 'onStart'); await call(pan, 'onUpdate', { translationX: 60, translationY: 70 });
  await call(zoom, 'onStart', { focalX: 60, focalY: 70 });
  for (let i = 0; i < 100; i++) await call(zoom, 'onUpdate', { focalX: 60, focalY: 70, scale: 3 });
  expect(mockSchedule).not.toHaveBeenCalled(); expect(mockDerived[0]()).toEqual([3, 0, 60, 0, 3, 70, 0, 0, 1]);
  await call(tap, 'onEnd', { x: 360, y: 670 }); expect(mockSchedule).not.toHaveBeenCalled(); // a pinch is not a tap
  await call(zoom, 'onFinalize'); expect(mockSchedule.mock.calls.map((c) => c[0].name)).toEqual(['settle']); mockSchedule.mockClear();
  await call(tap, 'onEnd', { x: 360, y: 670 }, false); expect(mockSchedule).not.toHaveBeenCalled();
  await call(tap, 'onEnd', { x: 360, y: 670 }); expect(p.onPick).toHaveBeenCalledWith(point.id); expect(mockSchedule).toHaveBeenCalledTimes(1);
  await call(pan, 'onUpdate', { translationX: 60, translationY: 70 });
  expect(mockDerived[0]()).toEqual([3, 0, 60, 0, 3, 70, 0, 0, 1]); // no jump after the second finger leaves
});
it('draws no glow with Reduce Motion and filters before drawing nodes or hit-testing', async () => {
  const p = props(); await render(<SkiaMindMap {...p} reduced until={50} />);
  expect(mockDraws.filter((d) => d.kind === 'Circle')).toHaveLength(0);
  await call(mockGestures[2], 'onEnd', { x: point.x, y: point.y }); expect(p.onPick).not.toHaveBeenCalled();
});
it('draws a recent glow normally and removes it under Reduce Motion', async () => {
  const p = props(), r = await render(<SkiaMindMap {...p} />); expect(mockDraws.some((d) => d.kind === 'BlurMask')).toBe(true);
  mockDraws.length = 0; await r.rerender(<SkiaMindMap {...p} reduced />); expect(mockDraws.some((d) => d.kind === 'BlurMask')).toBe(false);
});
it('draws connections as one path per kind and outlined labels that never overlap', async () => {
  const p = props();
  p.view = { ...p.view, nodes: [...p.view.nodes, { ...p.view.nodes[0], id: 'people/Will.md', folder: 'people', name: 'Will.md', title: 'Will Turner 2026-10-01' }],
    edges: [{ a: 'topics/rates.md', b: 'people/Will.md', kind: 'mention' }] };
  p.view.nodes[0].size = 1; p.view.nodes[1].size = 1;
  await render(<SkiaMindMap {...p} points={[point, { id: 'people/Will.md', x: 300, y: 200, r: 8 }]} />);
  await fireEvent(screen.getByTestId('mind-map-surface'), 'layout', { nativeEvent: { layout: { width: 1000, height: 1000 } } });
  const paths = mockDraws.filter((d) => d.kind === 'Path').slice(-2);
  expect(paths).toHaveLength(2);
  expect((paths.at(-1)!.props.path as { ops: unknown[] }).ops).toEqual([['M', 100, 200], ['L', 300, 200]]);
  const texts = mockDraws.filter((d) => d.kind === 'Text').slice(-4).map((d) => [d.props.text, d.props.style ?? 'fill']);
  expect(texts).toEqual([['Will Turner', 'stroke'], ['Will Turner', 'fill'], ['Rates', 'stroke'], ['Rates', 'fill']]);
});
it('offers Show all after he moves the map, and it puts the whole map back', async () => {
  const p = props(); await render(<SkiaMindMap {...p} />);
  await fireEvent(screen.getByTestId('mind-map-surface'), 'layout', { nativeEvent: { layout: { width: 1000, height: 1000 } } });
  expect(screen.queryByLabelText('Show the whole map')).toBeNull();
  const [pan] = mockGestures; await call(pan, 'onStart'); await call(pan, 'onUpdate', { translationX: 40, translationY: 0 }); await call(pan, 'onEnd');
  expect(mockDerived[0]()).toEqual([1, 0, 40, 0, 1, 0, 0, 0, 1]);
  await fireEvent.press(screen.getByLabelText('Show the whole map'));
  expect(mockDerived[0]()).toEqual([1, 0, 0, 0, 1, 0, 0, 0, 1]); expect(screen.queryByLabelText('Show the whole map')).toBeNull();
});
