// Tap to start, tap to finish (docs/archive/MOBILE-APP-PLAN.md M2), with what M0 learned on the phone:
// the session allows recording only while recording; the file is used only after iOS says
// it is finished; a 2-minute cap is a timer, not record({forDuration}) (which ignored whether
// the recorder started); and every player keeps the session active (audio.ts PLAYER_OPTIONS).
import {
  requestRecordingPermissionsAsync,
  setAudioModeAsync,
  useAudioRecorder,
  useAudioRecorderState,
  type RecordingStatus,
} from 'expo-audio';
import { File } from 'expo-file-system';
import * as Haptics from 'expo-haptics';
import { useCallback, useRef, useState } from 'react';

import { MAX_RECORDING_MS, RECORDING } from '../core/audio';

export type Recording = { uri: string; ms: number; bytes: number };
export type RecorderError = 'permission' | 'failed';

const PLAYBACK_MODE = { allowsRecording: false, playsInSilentMode: true, shouldPlayInBackground: true } as const;

export function useRecorder(onCap: () => void) {
  const onFinished = useRef<((s: RecordingStatus) => void) | null>(null);
  const recorder = useAudioRecorder(RECORDING, (status) => {
    if (status.isFinished) onFinished.current?.(status);
  });
  const state = useAudioRecorderState(recorder, 100);
  const cap = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [active, setActive] = useState(false);

  /** Starts recording; returns null on success, or why it could not. */
  const start = useCallback(async (): Promise<RecorderError | null> => {
    const perm = await requestRecordingPermissionsAsync();
    if (!perm.granted) return 'permission';
    try {
      await setAudioModeAsync({ allowsRecording: true, playsInSilentMode: true, interruptionMode: 'doNotMix' });
      await recorder.prepareToRecordAsync(RECORDING);     // with options: a new file each time
      recorder.record();
    } catch {
      await setAudioModeAsync(PLAYBACK_MODE).catch(() => undefined);
      return 'failed';
    }
    setActive(true);
    cap.current = setTimeout(onCap, MAX_RECORDING_MS);
    void Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium);
    return null;
  }, [recorder, onCap]);

  const finish = useCallback(async (): Promise<Recording | null> => {
    if (cap.current) clearTimeout(cap.current);
    cap.current = null;
    const ms = recorder.getStatus().durationMillis;
    const finished = new Promise<RecordingStatus | null>((resolve) => {
      onFinished.current = resolve;
      setTimeout(() => resolve(null), 2000);
    });
    await recorder.stop();
    const status = await finished;
    onFinished.current = null;
    setActive(false);
    await setAudioModeAsync(PLAYBACK_MODE).catch(() => undefined);
    void Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light);
    const uri = status?.url ?? recorder.uri;
    if (!uri || status?.hasError) return null;
    return { uri, ms, bytes: new File(uri).size ?? 0 };
  }, [recorder]);

  /** Stop and throw the recording away. */
  const cancel = useCallback(async () => {
    const rec = await finish();
    if (rec) {
      try {
        new File(rec.uri).delete();
      } catch {
        /* already gone */
      }
    }
  }, [finish]);

  return { start, finish, cancel, active, ms: state.durationMillis, metering: state.metering };
}
