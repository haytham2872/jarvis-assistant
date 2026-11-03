// The app: tabs (Chat, Activity) under one shared state, with Settings and past chats pushed on
// top. Dark theme throughout: on iOS 26+ the system tab bar is Liquid Glass and takes its look
// from what is behind it, so everything behind it must be dark.
import { DarkTheme, Stack, ThemeProvider } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { GestureHandlerRootView } from 'react-native-gesture-handler';
import { SafeAreaProvider } from 'react-native-safe-area-context';

import { AppProvider } from '../state/AppContext';
import { mark } from '../core/perf';
import { color } from '../theme/tokens';

const theme = { ...DarkTheme, colors: { ...DarkTheme.colors, background: color.bg, primary: color.stIdle } };
mark('app:start');

export default function RootLayout() {
  return (
    // Drag and pinch (the portfolio chart, the week) need the gesture root around everything.
    <GestureHandlerRootView style={{ flex: 1 }}>
    <SafeAreaProvider>
      <ThemeProvider value={theme}>
        <AppProvider>
          <StatusBar style="light" />
          <Stack screenOptions={{ headerShown: false, contentStyle: { backgroundColor: color.bg } }}>
            <Stack.Screen name="(tabs)" />
          </Stack>
        </AppProvider>
      </ThemeProvider>
    </SafeAreaProvider>
    </GestureHandlerRootView>
  );
}
