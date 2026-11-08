// The speaker (M3): SpeechQueue wired to expo-audio for the server's voice and expo-speech
// for the iPhone's. Clips are downloaded to the cache (as the M0 probe did on the phone)
// and played from the file; the same answer replayed reuses them.
import { setAudioModeAsync, useAudioPlayer } from 'expo-audio';
import { Directory, File, Paths } from 'expo-file-system';
import * as Speech from 'expo-speech';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AppState } from 'react-native';

import { fetchClipBytes, fetchLiveClip, getSpeechPlan } from '../chat/client';
import { PLAYER_OPTIONS } from '../core/audio';
import type { ServerConfig } from '../core/api';
import { playbackLevel, type LevelBus } from './levels';
import { IDLE, SpeechQueue, type Ended, type Lang, type LiveSpeechEvent, type SpeakState } from './speechQueue';

const LOCAL_VOICE: Record<Lang, string> = { en: 'en-US', fr: 'fr-FR' };

function clipFile(revision: string, seq: number): File {
  const dir = new Directory(Paths.cache, 'speech');
  if (!dir.exists) dir.create({ intermediates: true });
  return new File(dir, `${revision.replace(/[^\w-]/g, '')}-${seq}.mp3`);
}

function liveClipFile(requestId: string, seq: number): File {
  const dir = new Directory(Paths.cache, 'speech');
  if (!dir.exists) dir.create({ intermediates: true });
  return new File(dir, `live-${requestId.replace(/[^\w-]/g, '')}-${seq}.mp3`);
}

export function useSpeaker(cfg: ServerConfig | null, rate: number, levels?: LevelBus) {
  const player = useAudioPlayer(null, PLAYER_OPTIONS);
  const [state, setState] = useState<SpeakState>(IDLE);
  const cfgRef = useRef(cfg);
  cfgRef.current = cfg;
  const rateRef = useRef(rate);
  rateRef.current = rate;
  const playing = useRef<((e: Ended) => void) | null>(null);
  const speaking = useRef<((e: Ended) => void) | null>(null);

  const settle = (ref: typeof playing, ended: Ended) => {
    const resolve = ref.current;
    ref.current = null;
    resolve?.(ended);
  };

  useEffect(() => {
    const sub = player.addListener('playbackStatusUpdate', (s) => {
      if (s.didJustFinish) {
        settle(playing, 'done');
        levels?.push(0);
      }
    });
    return () => sub.remove();
  }, [player, levels]);

  // The real output level, for the orb. The iPhone's own voice has no tap, so it has none.
  useEffect(() => {
    if (!levels || !player.isAudioSamplingSupported) return;
    try {
      player.setAudioSamplingEnabled(true);
    } catch {
      return;
    }
    const sub = player.addListener('audioSampleUpdate', (s) => levels.push(playbackLevel(s)));
    return () => sub.remove();
  }, [player, levels]);

  const queue = useMemo(() => new SpeechQueue({
    plan: (source) => {
      if (!cfgRef.current) throw new Error('not paired');
      return getSpeechPlan(cfgRef.current, source);
    },
    fetchClip: async (source, revision, seq) => {
      const file = clipFile(revision, seq);
      if (file.exists && (file.size ?? 0) > 0) return file.uri;
      if (!cfgRef.current) throw new Error('not paired');
      const bytes = await fetchClipBytes(cfgRef.current, source, revision, seq);
      if (!file.exists) file.create();
      file.write(bytes);
      return file.uri;
    },
    fetchLive: async (source, seq) => {
      const id = source.replace(/^req:/, '');
      const file = liveClipFile(id, seq);
      if (file.exists && (file.size ?? 0) > 0) return file.uri;
      if (!cfgRef.current) throw new Error('not paired');
      const bytes = await fetchLiveClip(cfgRef.current, id, seq);
      if (!file.exists) file.create();
      file.write(bytes);
      return file.uri;
    },
    play: (uri) => new Promise<Ended>((resolve) => {
      settle(playing, 'stopped');
      playing.current = resolve;
      player.replace({ uri });
      player.play();
      player.setPlaybackRate(rateRef.current, 'high');
    }),
    speakLocal: (text, lang) => new Promise<Ended>((resolve) => {
      settle(speaking, 'stopped');
      speaking.current = resolve;
      Speech.speak(text, {
        language: LOCAL_VOICE[lang],
        rate: rateRef.current,
        onDone: () => settle(speaking, 'done'),
        onStopped: () => settle(speaking, 'stopped'),
        onError: () => settle(speaking, 'done'),
      });
    }),
    halt: () => {
      player.pause();
      levels?.push(0);
      settle(playing, 'stopped');
      void Speech.stop();
      settle(speaking, 'stopped');
    },
  }, setState), [player, levels]);

  useEffect(() => {
    // Playback that ignores the silent switch, without the microphone (the recorder turns it
    // on only while recording).
    void setAudioModeAsync({ allowsRecording: false, playsInSilentMode: true }).catch(() => undefined);
    // Expo Go cannot play in the background: leaving the app stops the answer.
    const sub = AppState.addEventListener('change', (s) => {
      if (s !== 'active') queue.stop();
    });
    return () => {
      sub.remove();
      queue.stop();
    };
  }, [queue]);

  const speak = useCallback((source: string, text = '') => void queue.speak(source, text), [queue]);
  /** A sentence (or the end) of a turn still being written: spoken as it arrives. */
  const live = useCallback((source: string, event: LiveSpeechEvent) => queue.pushLive(source, event), [queue]);
  const stop = useCallback(() => queue.stop(), [queue]);
  /** The speaker button: stop this answer if it is the one being read, else read it. */
  const toggle = useCallback((source: string, text = '') => {
    if (state.source === source && state.phase !== 'idle') queue.stop();
    else void queue.speak(source, text);
  }, [queue, state]);

  return { state, speak, live, stop, toggle };
}
