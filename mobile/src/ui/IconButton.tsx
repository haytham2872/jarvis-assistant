// A round glass button with an SF Symbol: the header's controls.
import { SymbolView, type SymbolViewProps } from 'expo-symbols';
import { Pressable, StyleSheet, Text } from 'react-native';

import { color, font } from '../theme/tokens';
import { Glass, type GlassTone } from './Glass';

type Props = {
  symbol: SymbolViewProps['name'];
  label: string;
  onPress: () => void;
  disabled?: boolean;
  tone?: GlassTone;
  size?: number;
};

export function IconButton({ symbol, label, onPress, disabled, tone = 'clear', size = 40 }: Props) {
  const tint = tone === 'accent' ? color.bg : color.tHi;
  return (
    <Pressable accessibilityRole="button" accessibilityLabel={label} onPress={onPress} disabled={disabled}
      hitSlop={6} style={({ pressed }) => [disabled && styles.off, pressed && styles.pressed]}>
      <Glass tone={tone} interactive style={[styles.round, { width: size, height: size, borderRadius: size / 2 }]}>
        <SymbolView name={symbol} size={Math.round(size * 0.45)} weight="semibold" tintColor={tint}
          fallback={<Text style={[styles.fallback, { color: tint }]}>{label[0]}</Text>} />
      </Glass>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  round: { alignItems: 'center', justifyContent: 'center' },
  fallback: { fontSize: font.body, fontWeight: '700' },
  off: { opacity: 0.4 },
  pressed: { opacity: 0.75 },
});
