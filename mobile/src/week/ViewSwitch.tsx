import { Pressable, Text, View } from 'react-native';
import { color } from '../theme/tokens';

export function ViewSwitch({ mode, onChange }: { mode: 'Day' | 'Week'; onChange: () => void }) {
  return <View style={{ flexDirection: 'row', borderRadius: 16, backgroundColor: color.surface2, padding: 3 }}>
    {(['Day', 'Week'] as const).map((label) => <Pressable key={label} accessibilityRole="button"
      accessibilityLabel={`${label} view`} accessibilityState={{ selected: mode === label }}
      onPress={mode === label ? undefined : onChange}
      style={{ flex: 1, minHeight: 40, alignItems: 'center', justifyContent: 'center', borderRadius: 13,
        backgroundColor: mode === label ? color.surface3 : 'transparent' }}>
      <Text style={{ color: mode === label ? color.tHi : color.tMid, fontWeight: '600', fontSize: 14 }}>{label}</Text>
    </Pressable>)}
  </View>;
}
