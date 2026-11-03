/// <reference types="node" />
// Guards the fix for silent recordings (14 October): a player that deactivates the iOS
// session on pause pauses the recorder 100 ms later. See PLAYER_OPTIONS in audio.ts. It used to
// read the M0 probe screen; since the probe was removed (15 October) it checks every player in
// the app, and that recording stops JARVIS's voice first.
import { readdirSync, readFileSync, statSync } from 'fs';
import { join } from 'path';

import { PLAYER_OPTIONS } from './audio';

jest.mock('expo-audio', () => ({ AudioQuality: { HIGH: 96 }, IOSOutputFormat: { MPEG4AAC: 'aac ' } }));

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return /\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name) ? [path] : [];
  });
}

const app = sources(join(__dirname, '..')).map((p) => readFileSync(p, 'utf8')).join('\n');

it('keeps the audio session active for every player in the app', () => {
  expect(PLAYER_OPTIONS.keepAudioSessionActive).toBe(true);
  const players = app.match(/useAudioPlayer\(|createAudioPlayer\(/g) ?? [];
  const guarded = app.match(/useAudioPlayer\([^)]*PLAYER_OPTIONS\)/g) ?? [];
  expect(players.length).toBeGreaterThan(0);
  expect(guarded.length).toBe(players.length);
});

it('stops JARVIS speaking before the microphone starts', () => {
  const chat = readFileSync(join(__dirname, '..', 'app', '(tabs)', 'index.tsx'), 'utf8');
  const start = chat.slice(chat.indexOf('const onRecordStart'), chat.indexOf('const onRecording'));
  expect(start).toContain('speaker.stop();');
});
