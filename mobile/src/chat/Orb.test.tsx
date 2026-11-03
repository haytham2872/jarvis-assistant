import { act, render, screen } from '@testing-library/react-native';
import { Animated } from 'react-native';

import { createLevelBus } from '../voice/levels';
import { Orb } from './Orb';

beforeEach(() => jest.restoreAllMocks());

it('says what JARVIS is doing, for VoiceOver and on screen', async () => {
  const bus = createLevelBus();
  await render(<Orb mode="thinking" bus={bus} reduced />);
  expect(screen.getByLabelText('JARVIS is thinking')).toBeTruthy();
  expect(screen.getByText('Thinking')).toBeTruthy();
  await screen.rerender(<Orb mode="idle" bus={bus} reduced />);
  expect(screen.getByLabelText('JARVIS is idle')).toBeTruthy();
  expect(screen.queryByText('Thinking')).toBeNull();
  await screen.rerender(<Orb mode="speaking" bus={bus} reduced showLabel={false} />);
  expect(screen.queryByText('Speaking')).toBeNull();
});

it('follows levels only while listening or speaking, without re-rendering', async () => {
  const bus = createLevelBus(0);
  const timing = jest.spyOn(Animated, 'timing');
  const renders = jest.fn();
  function Probe(p: { mode: 'idle' | 'listening' }) {
    renders();
    return <Orb mode={p.mode} bus={bus} reduced />;
  }
  await render(<Probe mode="idle" />);
  await act(() => bus.push(0.8));
  expect(timing).not.toHaveBeenCalled();
  await screen.rerender(<Probe mode="listening" />);
  const before = renders.mock.calls.length;
  await act(() => { bus.push(0.8); bus.push(0.3); });
  expect(timing).toHaveBeenCalledTimes(2);
  expect(timing.mock.calls[0][1]).toMatchObject({ toValue: 0.8, useNativeDriver: true });
  expect(renders.mock.calls.length).toBe(before);
});

it('breathes when idle and pulses when thinking, unless Reduce Motion is on', async () => {
  const bus = createLevelBus();
  const loop = jest.spyOn(Animated, 'loop');
  await render(<Orb mode="idle" bus={bus} reduced />);
  await screen.rerender(<Orb mode="thinking" bus={bus} reduced />);
  expect(loop).not.toHaveBeenCalled();
  await screen.rerender(<Orb mode="thinking" bus={bus} reduced={false} />);
  expect(loop).toHaveBeenCalledTimes(1);
  await screen.rerender(<Orb mode="speaking" bus={bus} reduced={false} />);   // a real level instead
  expect(loop).toHaveBeenCalledTimes(1);
});

function haloStyle() {
  return Object.assign({}, ...[screen.getByTestId('orb-halo').props.style].flat(3).filter(Boolean));
}

it('does not grow with Reduce Motion: the halo only brightens', async () => {
  // Levels land at once (the native driver does not run under Jest).
  jest.spyOn(Animated, 'timing').mockImplementation(((v: Animated.Value, cfg: { toValue: number }) =>
    ({ start: () => v.setValue(cfg.toValue), stop: () => undefined, reset: () => undefined })) as never);
  const bus = createLevelBus(0);
  await render(<Orb mode="listening" bus={bus} reduced />);
  const quiet = haloStyle().opacity;
  await act(() => bus.push(1));
  expect(haloStyle().transform).toEqual([{ scale: 1 }]);
  expect(haloStyle().opacity).toBeGreaterThan(quiet);
  await screen.rerender(<Orb mode="listening" bus={bus} reduced={false} />);
  await act(() => bus.push(1));
  expect(haloStyle().transform[0].scale).toBeGreaterThan(1.4);
});

it('is memoized, so the screen re-rendering on every streamed token leaves it alone', () => {
  expect((Orb as unknown as { $$typeof: symbol }).$$typeof).toBe(Symbol.for('react.memo'));
});
