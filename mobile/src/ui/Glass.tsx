// Liquid Glass (iOS 26+, expo-glass-effect) in JARVIS blue, for the controls that float over
// the conversation: header buttons, the composer, the approval tray. Content (answers, cards)
// stays on plain surfaces, as Apple's guidance puts glass on the control layer only.
//
// Nothing confirms Expo Go carries the glass module, and expo-glass-effect looks its native
// view up the moment it is imported, which would crash without it. So the module is looked up
// first (requireOptionalNativeModule never throws) and only then required. Without it, or with
// Reduce Transparency on, the same shapes render frosted: a translucent fill and a fine edge.
import { requireOptionalNativeModule } from 'expo-modules-core';
import { useEffect, useState, type ComponentType, type ReactNode } from 'react';
import { AccessibilityInfo, Platform, StyleSheet, View, type StyleProp, type ViewStyle } from 'react-native';

import { glass } from '../theme/tokens';

type GlassNative = { isLiquidGlassAvailable?: boolean; isGlassEffectAPIAvailable?: boolean };

/** True when this app can draw real Liquid Glass (iOS 26+, the module present, the API live). */
export function detectLiquidGlass(
  os: string = Platform.OS,
  lookup: (name: string) => GlassNative | null = (n) => requireOptionalNativeModule<GlassNative>(n),
): boolean {
  if (os !== 'ios') return false;
  try {
    const m = lookup('ExpoGlassEffect');
    // isGlassEffectAPIAvailable guards early iOS 26 betas where the API crashed.
    return !!m?.isLiquidGlassAvailable && m.isGlassEffectAPIAvailable !== false;
  } catch {
    return false;
  }
}

type NativeGlassView = ComponentType<Record<string, unknown>>;

/** expo-glass-effect's view, required only when the module is there (importing it without the
 * module would throw at once); null when it is not, or the require fails. */
export function loadNativeGlass(
  available: boolean,
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  req: () => { GlassView?: NativeGlassView } = () => require('expo-glass-effect'),
): NativeGlassView | null {
  if (!available) return null;
  try {
    return req().GlassView ?? null;
  } catch {
    return null;
  }
}

export const LIQUID_GLASS = detectLiquidGlass();
let NativeGlass = loadNativeGlass(LIQUID_GLASS);

/** Tests only: draw with this native view (or the frosted fallback with null). */
export function _setNativeGlass(view: NativeGlassView | null): void {
  NativeGlass = view;
}

export type GlassTone = 'clear' | 'blue' | 'accent' | 'amber';

export function useReduceTransparency(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    let alive = true;
    void AccessibilityInfo.isReduceTransparencyEnabled?.().then((r) => alive && setReduced(r)).catch(() => undefined);
    const sub = AccessibilityInfo.addEventListener('reduceTransparencyChanged', setReduced);
    return () => {
      alive = false;
      sub?.remove();
    };
  }, []);
  return reduced;
}

type Props = {
  tone?: GlassTone;
  /** Glass that reacts to touch (buttons). */
  interactive?: boolean;
  style?: StyleProp<ViewStyle>;
  children?: ReactNode;
  testID?: string;
};

export function Glass({ tone = 'clear', interactive = false, style, children, testID }: Props) {
  const reduced = useReduceTransparency();
  if (NativeGlass && !reduced) {
    return (
      <NativeGlass testID={testID} style={style} glassEffectStyle={tone === 'clear' ? 'clear' : 'regular'}
        tintColor={glass.tint[tone]} isInteractive={interactive} colorScheme="dark">
        {children}
      </NativeGlass>
    );
  }
  return (
    <View testID={testID} style={[styles.frost, { backgroundColor: glass.frost[tone] }, style]}>
      {children}
    </View>
  );
}

const styles = StyleSheet.create({
  frost: { borderColor: glass.edge, borderWidth: StyleSheet.hairlineWidth, overflow: 'hidden' },
});
