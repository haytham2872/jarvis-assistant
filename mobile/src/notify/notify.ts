// The notification side effects (slice 4): permission, scheduling what plan.ts decides, and the try
// at server pushes. Expo Go's notifications module is available on iOS; on the web preview every
// call here is a no-op.
import Constants from 'expo-constants';
import * as Device from 'expo-device';
import * as Notifications from 'expo-notifications';
import * as SecureStore from 'expo-secure-store';
import { Platform } from 'react-native';

import { confirmPushToken, registerPushToken } from '../chat/client';
import type { PushState, UpcomingItem } from '../chat/types';
import type { ServerConfig } from '../core/api';

const TOKEN_KEY = 'jarvis.push.v1';
export const supported = Platform.OS === 'ios' || Platform.OS === 'android';

export type Permission = 'granted' | 'denied' | 'undetermined';

/** Show JARVIS's notifications even while the app is open. */
export function configure(): void {
  if (!supported) return;
  Notifications.setNotificationHandler({
    handleNotification: async () => ({
      shouldShowBanner: true, shouldShowList: true, shouldPlaySound: true, shouldSetBadge: false,
    }),
  });
}

export async function permission(ask: boolean): Promise<Permission> {
  if (!supported) return 'denied';
  try {
    let { status } = await Notifications.getPermissionsAsync();
    if (status !== 'granted' && ask) ({ status } = await Notifications.requestPermissionsAsync());
    return status === 'granted' ? 'granted' : status === 'denied' ? 'denied' : 'undetermined';
  } catch {
    return 'undetermined';
  }
}

/** Replace what is scheduled with `items`. Returns how many are now scheduled. */
export async function replaceScheduled(items: UpcomingItem[]): Promise<number> {
  if (!supported) return 0;
  await Notifications.cancelAllScheduledNotificationsAsync();
  let n = 0;
  for (const it of items) {
    try {
      await Notifications.scheduleNotificationAsync({
        content: { title: it.title, body: it.body, sound: 'default', data: { screen: it.screen, kind: it.kind, upcoming: it.id } },
        trigger: { type: Notifications.SchedulableTriggerInputTypes.DATE, date: new Date(it.at * 1000) },
      });
      n += 1;
    } catch {
      /* one refused item leaves the rest */
    }
  }
  return n;
}

export async function loadPushToken(): Promise<string | null> {
  try {
    return await SecureStore.getItemAsync(TOKEN_KEY);
  } catch {
    return null;
  }
}

export type PushTry = { state: PushState | 'unavailable'; reason: string; token: string | null };

/** Ask Expo for this phone's push token and register it; the server then sends a test
 * notification, and pushes start only once that test shows here (onTestSeen). */
export async function tryPush(cfg: ServerConfig): Promise<PushTry> {
  if (Platform.OS !== 'ios') return { state: 'unavailable', reason: 'iPhone only', token: null };
  if (!Device.isDevice) return { state: 'unavailable', reason: 'not on a real phone', token: null };
  if ((await permission(true)) !== 'granted') return { state: 'unavailable', reason: 'notifications are off for Expo Go', token: null };
  const projectId = (Constants.expoConfig?.extra as { eas?: { projectId?: string } } | undefined)?.eas?.projectId;
  let token: string;
  try {
    token = (await Notifications.getExpoPushTokenAsync(projectId ? { projectId } : undefined)).data;
  } catch (e) {
    return { state: 'unavailable', reason: (e as Error)?.message?.slice(0, 120) || 'no push token', token: null };
  }
  try {
    await SecureStore.setItemAsync(TOKEN_KEY, token);
  } catch {
    /* kept for this session only */
  }
  const r = await registerPushToken(cfg, token);
  return { state: r.push, reason: '', token };
}

/** The test notification arrived: tell the server pushes reach this phone. */
export async function onTestSeen(cfg: ServerConfig, token: string): Promise<PushState> {
  return (await confirmPushToken(cfg, token)).push;
}
