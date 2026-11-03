import { act, render } from '@testing-library/react-native';
import { Text } from 'react-native';
import { Drag, EmptyGrid, type DragProps } from './NativeGesture';

type Event = { translationX: number; translationY: number; x: number; y: number };
type G = Record<string, jest.Mock>;
const mockGestures: G[] = [], mockStyles: (() => Record<string, unknown>)[] = [];
const mockSchedule = jest.fn(), mockTiming = jest.fn((n: number) => n);
jest.mock('react-native-worklets', () => ({ scheduleOnRN: (...args: unknown[]) => mockSchedule(...args) }));
jest.mock('react-native-gesture-handler', () => {
  const build = () => {
    const g: G = {};
    for (const k of ['enabled', 'activateAfterLongPress', 'activeOffsetX', 'failOffsetY', 'onStart', 'onUpdate', 'onEnd', 'onFinalize', 'minDuration', 'maxDistance'])
      g[k] = jest.fn(() => g);
    mockGestures.push(g); return g;
  };
  return { Gesture: { Pan: build, LongPress: build, Race: jest.fn((...gs) => gs) }, GestureDetector: ({ children }: { children: unknown }) => children };
});
jest.mock('react-native-reanimated', () => {
  const R = require('react'), { View } = require('react-native');
  return { __esModule: true, default: { View }, useSharedValue: (value: unknown) => R.useRef({ value,
    get() { return this.value; }, set(v: unknown) { this.value = v; } }).current,
    useAnimatedStyle: (fn: () => Record<string, unknown>) => { mockStyles.push(fn); return {}; }, withTiming: (n: number) => mockTiming(n) };
});
const props = (): DragProps => ({ uid: 'gym', index: 0, minute: 540, duration: 60, width: 100, bases: [0, 86400, 172800],
  busy: [], buffer: 0, lo: 420, hi: 1380, disabled: false, reduced: false, onDrop: jest.fn(), onPage: jest.fn(), children: <Text>Gym</Text> });
const event = (x = 0, y = 0): Event => ({ translationX: x, translationY: y, x, y });
async function call(g: G, name: string, e = event(), success = true) { await act(() => g[name].mock.calls[0][0](e, success)); }
beforeEach(() => { mockGestures.length = 0; mockStyles.length = 0; mockSchedule.mockClear(); mockTiming.mockClear(); });

it('races a 350ms hold against horizontal swipe and crosses to JS only on release', async () => {
  const p = props(); await render(<Drag {...p} />);
  const [drag, swipe] = mockGestures;
  expect(drag.activateAfterLongPress).toHaveBeenCalledWith(350);
  expect(swipe.failOffsetY).toHaveBeenCalledWith([-14, 14]);
  await call(drag, 'onStart');
  for (let i = 0; i < 300; i++) await call(drag, 'onUpdate', event(100, 80));
  expect(mockSchedule).not.toHaveBeenCalled();
  expect(mockStyles[0]().transform).toEqual([{ translateX: 100 }, { translateY: 80 }]);
  await call(drag, 'onEnd');
  expect(mockSchedule).toHaveBeenCalledTimes(1);
  expect(mockSchedule).toHaveBeenCalledWith(p.onDrop, 'gym', 1, 615);
  await call(drag, 'onFinalize');
  expect(mockSchedule).toHaveBeenCalledTimes(1);
});
it('marks an overlap red on the UI thread and does not commit cancellation', async () => {
  await render(<Drag {...props()} busy={[{ uid: 'meeting', start: 7200, end: 14400, event: true }]} />);
  const [drag] = mockGestures;
  await call(drag, 'onStart'); await call(drag, 'onUpdate');
  expect(mockStyles[0]().borderColor).toBe('#e5604d');
  await call(drag, 'onEnd', event(), false); await call(drag, 'onFinalize', event(), false);
  expect(mockSchedule).not.toHaveBeenCalled();
  expect(mockTiming).toHaveBeenCalledTimes(2);
});
it('returns without animation with Reduce Motion and pages only after a swipe release', async () => {
  const p = props(); await render(<Drag {...p} reduced />);
  await call(mockGestures[0], 'onFinalize', event(), false);
  expect(mockTiming).not.toHaveBeenCalled();
  await call(mockGestures[1], 'onEnd', event(-100));
  expect(mockSchedule).toHaveBeenCalledWith(p.onPage, 1);
});
it('opens an empty slot only after a successful hold release', async () => {
  const add = jest.fn(), page = jest.fn(); await render(<EmptyGrid disabled={false} onAdd={add} onPage={page}><Text>Grid</Text></EmptyGrid>);
  expect(mockGestures[0].minDuration).toHaveBeenCalledWith(350);
  await call(mockGestures[0], 'onEnd', event(15, 30), false);
  expect(mockSchedule).not.toHaveBeenCalled();
  await call(mockGestures[0], 'onEnd', event(15, 30));
  expect(mockSchedule).toHaveBeenCalledWith(add, 15, 30);
});

it('still permits swiping across a week whose edits are disabled', async () => {
  const p = props(); await render(<Drag {...p} disabled />);
  expect(mockGestures[0].enabled).toHaveBeenCalledWith(false);
  expect(mockGestures[1].enabled).not.toHaveBeenCalledWith(false);
  await call(mockGestures[1], 'onEnd', event(-100));
  expect(mockSchedule).toHaveBeenCalledWith(p.onPage, 1);
});
