// Recording and playback settings for the probe (docs/archive/MOBILE-APP-PLAN.md section 6).
import { AudioQuality, IOSOutputFormat, type RecordingOptions } from 'expo-audio';

/** 16 kHz mono AAC in .m4a: what Parakeet uses, and small (2 min at 32 kbit/s is <1 MB). */
export const RECORDING: RecordingOptions = {
  extension: '.m4a',
  sampleRate: 16000,
  numberOfChannels: 1,
  bitRate: 32000,
  isMeteringEnabled: true,
  android: { outputFormat: 'mpeg4', audioEncoder: 'aac' },
  ios: { outputFormat: IOSOutputFormat.MPEG4AAC, audioQuality: AudioQuality.HIGH },
  web: { mimeType: 'audio/webm', bitsPerSecond: 32000 },
};

export const MAX_RECORDING_MS = 120_000;

/**
 * Every player keeps the iOS audio session active. Otherwise expo-audio deactivates the
 * session 100 ms after a player pauses or finishes, and pauses every recorder when it does
 * (expo-audio/ios/AudioModule.swift, deactivateSession). Found 14 October on the phone:
 * recording started right after player.pause(), every file held 0.38 s of silence and the
 * meter read -120 dB throughout. The app sets the session itself (setAudioModeAsync).
 */
export const PLAYER_OPTIONS = { keepAudioSessionActive: true } as const;
