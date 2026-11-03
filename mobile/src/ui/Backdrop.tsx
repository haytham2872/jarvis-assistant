// The background every screen sits on: deep blue falling to near-black, lit faintly from the
// top. Glass needs something behind it to bend; a flat colour would make it invisible.
import { LinearGradient } from 'expo-linear-gradient';
import { StyleSheet, View } from 'react-native';

import { backdrop } from '../theme/tokens';

export function Backdrop() {
  return (
    <View style={StyleSheet.absoluteFill} pointerEvents="none" testID="backdrop">
      <LinearGradient colors={backdrop.colors} locations={backdrop.locations} style={StyleSheet.absoluteFill} />
      <LinearGradient colors={backdrop.glow} start={{ x: 0.15, y: 0 }} end={{ x: 0.55, y: 0.45 }}
        style={StyleSheet.absoluteFill} />
    </View>
  );
}
