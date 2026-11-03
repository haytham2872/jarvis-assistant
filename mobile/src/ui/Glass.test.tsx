import { render, screen } from '@testing-library/react-native';
import { View } from 'react-native';

import { _setNativeGlass, detectLiquidGlass, Glass, loadNativeGlass } from './Glass';

afterEach(() => _setNativeGlass(null));

it('uses real Liquid Glass only on iOS with the module present and its API live', () => {
  const lookup = (m: object | null) => () => m;
  expect(detectLiquidGlass('ios', lookup({ isLiquidGlassAvailable: true, isGlassEffectAPIAvailable: true }))).toBe(true);
  expect(detectLiquidGlass('ios', lookup({ isLiquidGlassAvailable: true }))).toBe(true);
  expect(detectLiquidGlass('ios', lookup({ isLiquidGlassAvailable: true, isGlassEffectAPIAvailable: false }))).toBe(false);
  expect(detectLiquidGlass('ios', lookup({ isLiquidGlassAvailable: false }))).toBe(false);
  expect(detectLiquidGlass('ios', lookup(null))).toBe(false);              // Expo Go without the module
  expect(detectLiquidGlass('android', lookup({ isLiquidGlassAvailable: true }))).toBe(false);
});

it('never crashes when the module lookup itself throws', () => {
  expect(detectLiquidGlass('ios', () => { throw new Error('no native modules'); })).toBe(false);
});

it('requires expo-glass-effect only when the module is there, and survives a failed require', () => {
  const req = jest.fn(() => ({ GlassView: View as never }));
  expect(loadNativeGlass(false, req)).toBeNull();
  expect(req).not.toHaveBeenCalled();
  expect(loadNativeGlass(true, req)).toBe(View);
  expect(loadNativeGlass(true, () => { throw new Error('Cannot find native module ExpoGlassEffect'); })).toBeNull();
});

it('without native glass, draws a frosted surface in the tone asked for', async () => {
  _setNativeGlass(null);
  await render(<Glass tone="accent" testID="g" />);
  const style = Object.assign({}, ...[screen.getByTestId('g').props.style].flat(3).filter(Boolean));
  expect(style.backgroundColor).toBe('#2fb6f0');
});

it('with it, draws the native glass tinted JARVIS blue and reacting to touch', async () => {
  const seen: Record<string, unknown>[] = [];
  _setNativeGlass((props: Record<string, unknown>) => {
    seen.push(props);
    return <View testID={props.testID as string} />;
  });
  await render(<Glass tone="accent" interactive testID="g" />);
  expect(screen.getByTestId('g')).toBeTruthy();
  expect(seen[0]).toMatchObject({ tintColor: 'rgba(47,182,240,0.78)', isInteractive: true, colorScheme: 'dark',
    glassEffectStyle: 'regular' });
});

it('with Reduce Transparency on, draws frosted even where native glass exists', async () => {
  const { AccessibilityInfo } = jest.requireActual('react-native');
  const spy = jest.spyOn(AccessibilityInfo, 'isReduceTransparencyEnabled').mockResolvedValue(true);
  const native = jest.fn(() => <View testID="native" />);
  _setNativeGlass(native);
  await render(<Glass tone="blue" testID="g" />);
  expect(await screen.findByTestId('g')).toBeTruthy();
  const style = Object.assign({}, ...[screen.getByTestId('g').props.style].flat(3).filter(Boolean));
  expect(style.backgroundColor).toBe('rgba(18,46,70,0.85)');
  expect(screen.queryByTestId('native')).toBeNull();
  spy.mockRestore();
});
