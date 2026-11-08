// The live microphone: expo-audio's PCM stream (16 kHz mono int16) goes to the server over the live
// voice socket while the owner talks, and a WAV of the same audio is kept alongside so a failed
// socket can fall back to today's upload (POST /recordings, same request id).
//
// `start` says 'unavailable' (the caller then uses useRecorder as before) when the stream cannot
// run: the native module is missing from this Expo Go binary (stream.start() throws), no audio
// arrives, the rate is not 16 kHz, or the WAV cannot be written.
// The stream puts the iOS session in .record/.measurement, so `finish` always restores playback.
import {
  requestRecordingPermissionsAsync,
  setAudioModeAsync,
  useAudioStream,
  type AudioStreamBuffer,
} from 'expo-audio';
import { File } from 'expo-file-system';
import * as Haptics from 'expo-haptics';
import { useCallback, useEffect, useRef, useState } from 'react';

import { authHeaders, type ServerConfig } from '../core/api';
import { MAX_RECORDING_MS } from '../core/audio';
import { liveUrl } from '../chat/client';
import { int16Level, LiveVoice, type LiveResult } from './liveVoice';

const PLAYBACK_MODE = { allowsRecording: false, playsInSilentMode: true, shouldPlayInBackground: true } as const;
const FIRST_BUFFER_MS = 2000;
const RATE = 16000;

export type LiveParams = { requestId: string; mode: 'conversation' | 'note'; spoken: boolean; replyTo?: number };
export type LiveStart =
  | { ok: true; result: Promise<LiveResult> }
  | { ok: false; why: 'permission' | 'unavailable' };
export type LiveFile = { uri: string; ms: number };

export function useLiveMic(cfg: ServerConfig | null | undefined, onCap: () => void) {
  const session = useRef<LiveVoice | null>(null);
  const first = useRef<((rate: number) => void) | null>(null);
  const fileP = useRef<Promise<LiveFile | null> | null>(null);
  const cap = useRef<ReturnType<typeof setTimeout> | null>(null);
  const began = useRef(0);
  const [active, setActive] = useState(false);
  const [heard, setHeard] = useState('');
  const [ms, setMs] = useState(0);
  const [metering, setMetering] = useState<number | undefined>(undefined);

  const onBuffer = useRef<(b: AudioStreamBuffer) => void>(() => undefined);
  const { stream } = useAudioStream({
    sampleRate: RATE, channels: 1, encoding: 'int16',
    onBuffer: (b) => onBuffer.current(b),
  });
  onBuffer.current = (b) => {
    first.current?.(b.sampleRate);
    first.current = null;
    if (b.sampleRate !== RATE) return;       // start() is tearing down
    session.current?.push(b.data);
    setMetering(int16Level(b.data));
    setMs(Date.now() - began.current);
  };

  /** Stops the stream and the WAV, restores playback; returns the WAV. Safe to call twice. */
  const finish = useCallback((): Promise<LiveFile | null> => {
    if (fileP.current) return fileP.current;
    if (cap.current) clearTimeout(cap.current);
    cap.current = null;
    const took = Date.now() - began.current;
    const p = (async () => {
      let file: LiveFile | null = null;
      try {
        const r = await stream.stopFileRecordingAsync();
        file = { uri: r.uri, ms: Math.round(r.duration * 1000) || took };
      } catch {
        /* no file: the live result is all there is */
      }
      try {
        stream.stop();
      } catch {
        /* already stopped */
      }
      setActive(false);
      await setAudioModeAsync(PLAYBACK_MODE).catch(() => undefined);
      void Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light);
      return file;
    })();
    fileP.current = p;
    return p;
  }, [stream]);

  const teardown = useCallback(async () => {
    first.current = null;
    session.current?.cancel();
    session.current = null;
    await finish().catch(() => null);
  }, [finish]);

  const start = useCallback(async (params: LiveParams): Promise<LiveStart> => {
    if (!cfg) return { ok: false, why: 'unavailable' };
    const perm = await requestRecordingPermissionsAsync();
    if (!perm.granted) return { ok: false, why: 'permission' };
    fileP.current = null;
    setHeard('');
    setMs(0);
    setMetering(undefined);
    began.current = Date.now();
    let live: LiveVoice;
    try {
      await setAudioModeAsync({ allowsRecording: true, playsInSilentMode: true, interruptionMode: 'doNotMix' });
      live = new LiveVoice({ url: liveUrl(cfg, params), headers: authHeaders(cfg.token), onHeard: setHeard });
    } catch {
      await setAudioModeAsync(PLAYBACK_MODE).catch(() => undefined);
      return { ok: false, why: 'unavailable' };
    }
    session.current = live;
    let wait: ReturnType<typeof setTimeout> | undefined;
    const rate = new Promise<number>((resolve) => {
      first.current = resolve;
      wait = setTimeout(() => resolve(0), FIRST_BUFFER_MS);
    });
    try {
      await stream.start();
      await stream.startFileRecordingAsync({ format: 'wav' });
      if ((await rate) !== RATE) throw new Error('no 16 kHz audio');
      clearTimeout(wait);
    } catch {
      clearTimeout(wait);
      await teardown();
      return { ok: false, why: 'unavailable' };
    }
    setActive(true);
    cap.current = setTimeout(onCap, MAX_RECORDING_MS);
    void Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium);
    return { ok: true, result: live.result };
  }, [cfg, stream, onCap, teardown]);

  /** Commit the turn now: the mic stops, the server answers with the turn. */
  const end = useCallback(async () => {
    session.current?.end();
    await finish();
  }, [finish]);

  /** Throw the turn away: the server drops it, the WAV is deleted. */
  const cancel = useCallback(async () => {
    session.current?.cancel();
    session.current = null;
    const file = await finish();
    if (file) {
      try {
        new File(file.uri).delete();
      } catch {
        /* already gone */
      }
    }
  }, [finish]);

  useEffect(() => () => {
    if (session.current) void teardown();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { start, end, cancel, finish, active, heard, ms, metering };
}
