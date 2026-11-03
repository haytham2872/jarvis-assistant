import { DEFAULT_PREFS, parsePrefs } from './prefs';

it('reads saved voice preferences and falls back on anything unexpected', () => {
  expect(parsePrefs(null)).toEqual(DEFAULT_PREFS);
  expect(parsePrefs('not json')).toEqual(DEFAULT_PREFS);
  expect(parsePrefs(JSON.stringify({ speakVoiceReplies: false, rate: 1.25 }))).toEqual({ speakVoiceReplies: false, rate: 1.25 });
  expect(parsePrefs(JSON.stringify({ speakVoiceReplies: 'yes', rate: 7 }))).toEqual(DEFAULT_PREFS);
  expect(DEFAULT_PREFS.speakVoiceReplies).toBe(true);
});
