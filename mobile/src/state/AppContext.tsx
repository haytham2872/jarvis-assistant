// What every screen shares (slice 1): the server connection, voice preferences, the phone chat,
// JARVIS's voice and the level bus the orb follows. It lived inside the chat screen until tabs
// arrived; another tab (Activity, later Today) needs the same connection and the same voice.
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { AppState } from 'react-native';

import type { ServerConfig } from '../core/api';
import { loadSettings } from '../core/settings';
import { waitingApprovals } from '../chat/reducer';
import { useChat } from '../chat/useChat';
import { useNotices, type Notices } from '../notify/useNotices';
import { createLevelBus, type LevelBus } from '../voice/levels';
import { DEFAULT_PREFS, loadPrefs, type VoicePrefs } from '../voice/prefs';
import { useSpeaker } from '../voice/useSpeaker';

type App = {
  cfg: ServerConfig | null;
  /** Settings have been read at least once (so "not paired" is not shown by mistake). */
  checked: boolean;
  prefs: VoicePrefs;
  /** Read settings and preferences again (after pairing, or a change in Settings). */
  reload: () => Promise<void>;
  levels: LevelBus;
  recording: boolean;
  setRecording: (on: boolean) => void;
  speaker: ReturnType<typeof useSpeaker>;
  chat: ReturnType<typeof useChat>;
  /** Approval requests waiting on the owner (the Activity tab's badge). */
  waitingCount: number;
  /** Slice 4: the Inbox's count, where JARVIS's own messages go, and notifications. */
  notices: Notices;
};

const Ctx = createContext<App | null>(null);

export function AppProvider({ children }: { children: ReactNode }) {
  const [cfg, setCfg] = useState<ServerConfig | null>(null);
  const [checked, setChecked] = useState(false);
  const [prefs, setPrefs] = useState<VoicePrefs>(DEFAULT_PREFS);
  const [levels] = useState(() => createLevelBus());
  const [recording, setRecording] = useState(false);

  const reload = useCallback(async () => {
    const [s, p] = await Promise.all([loadSettings(), loadPrefs()]);
    setCfg((old) => (s.baseUrl && s.token
      ? (old && old.baseUrl === s.baseUrl && old.token === s.token ? old : { baseUrl: s.baseUrl, token: s.token })
      : null));
    setPrefs(p);
    setChecked(true);
  }, []);

  useEffect(() => {
    void reload();
    const sub = AppState.addEventListener('change', (s) => s === 'active' && void reload());
    return () => sub.remove();
  }, [reload]);

  const speaker = useSpeaker(cfg, prefs.rate, levels);
  const chat = useChat(cfg, { onSpokenAnswer: speaker.speak });
  const notices = useNotices(cfg, chat.state.lastNotice);
  const waitingCount = useMemo(
    () => waitingApprovals(chat.state).filter((a) => a.state === 'pending').length, [chat.state]);

  const value = useMemo<App>(() => ({
    cfg, checked, prefs, reload, levels, recording, setRecording, speaker, chat, waitingCount, notices,
  }), [cfg, checked, prefs, reload, levels, recording, speaker, chat, waitingCount, notices]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useApp(): App {
  const app = useContext(Ctx);
  if (!app) throw new Error('useApp outside AppProvider');
  return app;
}
