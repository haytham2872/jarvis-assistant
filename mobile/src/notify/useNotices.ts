// The Inbox's shared state (slice 4): how many messages he has not seen, where JARVIS's own messages
// go, whether server pushes reach this phone, and what is scheduled here. Lives in AppContext so the
// tab badge, the Inbox and Settings agree.
import * as Notifications from 'expo-notifications';
import { router } from 'expo-router';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AppState } from 'react-native';

import { getInbox, getUpcoming, markInboxRead, setNoticeRoute } from '../chat/client';
import type { Notice, NoticeRoute, PushState } from '../chat/types';
import type { ServerConfig } from '../core/api';
import { configure, loadPushToken, onTestSeen, permission, replaceScheduled, supported, tryPush, type Permission } from './notify';
import { phoneIsTold, screenFor, toSchedule } from './plan';

const REFRESH_MS = 60_000;

export type Notices = {
  unread: number;
  route: NoticeRoute;
  push: PushState | 'unavailable';
  pushReason: string;
  permission: Permission;
  /** How many notifications this phone has scheduled itself. */
  scheduled: number;
  pushToken: string | null;
  refresh: () => Promise<void>;
  setRoute: (route: NoticeRoute) => Promise<string>;
  markRead: (upTo: number) => Promise<void>;
  retryPush: () => Promise<void>;
};

export function useNotices(cfg: ServerConfig | null, lastNotice: Notice | null): Notices {
  const [unread, setUnread] = useState(0);
  const [route, setRouteState] = useState<NoticeRoute>('discord');
  const [push, setPush] = useState<PushState | 'unavailable'>('unknown');
  const [pushReason, setPushReason] = useState('');
  const [perm, setPerm] = useState<Permission>('undetermined');
  const [scheduled, setScheduled] = useState(0);
  const [pushToken, setPushToken] = useState<string | null>(null);
  const tokenRef = useRef<string | null>(null);
  const scheduledSig = useRef<string | null>(null);   // what was last scheduled: unchanged, left alone
  const cfgRef = useRef(cfg);
  cfgRef.current = cfg;

  // The push token is read from the keychain once; a refresh waits for it, or the first one on
  // opening would not know pushes reach this phone and would schedule the same things here.
  const tokenLoaded = useRef<Promise<void> | null>(null);
  if (tokenLoaded.current === null) {
    tokenLoaded.current = loadPushToken().then((t) => {
      tokenRef.current = t;
      setPushToken(t);
    });
  }
  useEffect(() => {
    configure();
  }, []);

  /** Read the count and the settings, and schedule what this phone should notify about. */
  const refresh = useCallback(async () => {
    if (!cfg) return;
    try {
      await tokenLoaded.current;
      const inbox = await getInbox(cfg, tokenRef.current, { limit: 1 });
      setUnread(inbox.unread);
      setRouteState(inbox.route);
      if (tokenRef.current) setPush(inbox.push);
      if (!supported) return;
      const told = phoneIsTold(inbox.route);
      const p = await permission(told);
      setPerm(p);
      const want = told && p === 'granted'
        ? toSchedule((await getUpcoming(cfg, tokenRef.current)).items, inbox.route,
          tokenRef.current ? inbox.push : 'unknown', Date.now() / 1000)
        : [];
      const sig = want.map((i) => `${i.id}@${i.at}`).join('|');
      if (sig !== scheduledSig.current) {
        setScheduled(await replaceScheduled(want));
        scheduledSig.current = sig;
      }
    } catch {
      /* unreachable: keep what is scheduled; the next refresh tries again */
    }
  }, [cfg]);

  const doTryPush = useCallback(async () => {
    if (!cfg) return;
    try {
      const r = await tryPush(cfg);
      setPush(r.state);
      setPushReason(r.reason);
      if (r.token) {
        tokenRef.current = r.token;
        setPushToken(r.token);
      }
    } catch (e) {
      setPush('unavailable');
      setPushReason((e as Error)?.message?.slice(0, 120) ?? '');
    }
  }, [cfg]);

  useEffect(() => {
    void refresh();
    const t = setInterval(() => void refresh(), REFRESH_MS);
    const sub = AppState.addEventListener('change', (s) => s === 'active' && void refresh());
    return () => {
      clearInterval(t);
      sub.remove();
    };
  }, [refresh]);

  // A message announced live: the count moves at once, the rest is read with the next refresh.
  const seen = useRef<number | null>(null);
  useEffect(() => {
    if (!lastNotice || seen.current === lastNotice.id) return;
    seen.current = lastNotice.id;
    void refresh();
  }, [lastNotice, refresh]);

  // The test notification (shown, or tapped) confirms pushes; a tap opens the message's screen.
  useEffect(() => {
    if (!supported) return;
    const confirm = async (data: Record<string, unknown> | undefined) => {
      const c = cfgRef.current;
      if (!c || !data?.test || typeof data.token !== 'string') return;
      try {
        setPush(await onTestSeen(c, data.token));
        void refresh();
      } catch {
        /* the next test will tell */
      }
    };
    const open = (data: Record<string, unknown> | undefined) => {
      const screen = screenFor(data);
      if (screen) router.navigate(screen);
    };
    const received = Notifications.addNotificationReceivedListener((n) =>
      void confirm(n.request.content.data as Record<string, unknown>));
    const tapped = Notifications.addNotificationResponseReceivedListener((r) => {
      const data = r.notification.request.content.data as Record<string, unknown>;
      void confirm(data);
      open(data);
    });
    void Notifications.getLastNotificationResponseAsync().then((r) => {
      if (r) open(r.notification.request.content.data as Record<string, unknown>);
    }).catch(() => undefined);
    return () => {
      received.remove();
      tapped.remove();
    };
  }, [refresh]);

  const setRoute = useCallback(async (next: NoticeRoute): Promise<string> => {
    if (!cfg) return 'Not paired.';
    try {
      const r = await setNoticeRoute(cfg, next, tokenRef.current);
      setRouteState(r.route);
      if (phoneIsTold(r.route) && !tokenRef.current) await doTryPush();
      await refresh();
      return '';
    } catch (e) {
      return (e as Error)?.message ?? 'Could not save that.';
    }
  }, [cfg, refresh, doTryPush]);

  const markRead = useCallback(async (upTo: number) => {
    if (!cfg) return;
    try {
      setUnread((await markInboxRead(cfg, upTo)).unread);
    } catch {
      /* stays unread until the next try */
    }
  }, [cfg]);

  const retryPush = useCallback(async () => {
    await doTryPush();
    await refresh();
  }, [doTryPush, refresh]);

  return useMemo(() => ({
    unread, route, push, pushReason, permission: perm, scheduled, pushToken, refresh, setRoute, markRead, retryPush,
  }), [unread, route, push, pushReason, perm, scheduled, pushToken, refresh, setRoute, markRead, retryPush]);
}
