import { router } from 'expo-router';
import * as Haptics from 'expo-haptics';
import { type ReactNode } from 'react';
import { ActivityIndicator, Modal, Pressable, RefreshControl, ScrollView, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { HttpError } from '../chat/client';
import { useReduceMotion } from '../chat/Orb';
import { describeFailure, ExplainedError } from '../core/api';
import { color, space } from '../theme/tokens';
import { Backdrop } from '../ui/Backdrop';
import { Glass } from '../ui/Glass';
import { IconButton } from '../ui/IconButton';
import { styles as s } from './styles';

export function explainGoal(error: unknown) {
  if (error instanceof ExplainedError) return error.message;
  if (error instanceof HttpError && error.status === 404 && error.message === 'Not Found')
    return 'This server does not have the Goals view yet. Update it and restart JARVIS.';
  if (error instanceof HttpError && [400, 404, 409, 413, 422, 503].includes(error.status)) return error.message;
  const e = error as { status?: number; message?: string };
  return describeFailure(e?.status ?? null, e?.message ?? '');
}
export function Button({ label, accessibilityLabel, onPress, disabled, accent = false }:
  { label: string; accessibilityLabel?: string; onPress: () => void; disabled?: boolean; accent?: boolean }) {
  return <Pressable accessibilityRole="button" accessibilityLabel={accessibilityLabel ?? label}
    onPress={() => { void Haptics.selectionAsync(); onPress(); }} disabled={disabled}
    style={({ pressed }) => [{ maxWidth: '100%' }, disabled && s.off, pressed && s.pressed]}>
    <Glass interactive tone={accent ? 'blue' : 'clear'} style={s.button}><Text style={s.buttonText}>{label}</Text></Glass>
  </Pressable>;
}
export function Screen({ title, children, error, stale, loading, reload, refreshing = false, loadingLabel = 'Loading goal data' }:
  { title: string; children: ReactNode; error?: string; stale?: string; loading?: boolean; reload?: () => void; refreshing?: boolean; loadingLabel?: string }) {
  const insets = useSafeAreaInsets();
  return <View style={[s.screen, { paddingTop: insets.top }]}><Backdrop />
    <View style={s.header}><IconButton symbol="chevron.left" label="Back" onPress={() => router.canGoBack() ? router.back() : router.replace('/activity')} />
      <Text style={s.title}>{title}</Text></View>
    <ScrollView keyboardShouldPersistTaps="handled" contentContainerStyle={[s.content, { paddingBottom: insets.bottom + space.s6 }]}
      refreshControl={reload ? <RefreshControl refreshing={refreshing} onRefresh={reload} tintColor={color.stIdle} /> : undefined}>
      {error ? <Text style={s.error} accessibilityRole="alert">{error}</Text> : null}
      {stale ? <Text style={s.dim}>{stale}</Text> : null}
      {loading ? <ActivityIndicator color={color.stIdle} accessibilityLabel={loadingLabel} /> : null}
      {children}
    </ScrollView>
  </View>;
}
export function Sheet({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const insets = useSafeAreaInsets();
  const reduced = useReduceMotion();
  return <Modal transparent visible animationType={reduced ? 'none' : 'fade'} onRequestClose={onClose}>
    <View style={s.overlay}><Pressable accessibilityRole="button" accessibilityLabel="Close sheet" onPress={onClose} style={{ flex: 1 }} />
      <View style={s.sheet} accessibilityViewIsModal><View style={s.header}><Text style={s.title}>{title}</Text>
        <IconButton symbol="xmark" label="Close" onPress={onClose} /></View>
        <ScrollView contentContainerStyle={[s.content, { paddingBottom: insets.bottom + space.s5 }]}>{children}</ScrollView>
      </View>
    </View>
  </Modal>;
}
