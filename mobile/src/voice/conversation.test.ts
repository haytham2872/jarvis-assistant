import { afterSend, isGoodbye, SPEECH_START_GRACE_MS, Vad, VAD } from './conversation';

/** Feed `db` every 100 ms for `ms`, starting at `t0`; returns the first event and when. */
function run(vad: Vad, t0: number, ms: number, db: number): { ev: ReturnType<Vad['feed']>; at: number; t: number } {
  let t = t0;
  for (; t < t0 + ms; t += 100) {
    const ev = vad.feed(db, t);
    if (ev) return { ev, at: t, t: t + 100 };
  }
  return { ev: null, at: -1, t };
}

it('a turn ends after he speaks and then goes quiet for long enough', () => {
  const vad = new Vad();
  let r = run(vad, 0, VAD.calibrateMs + 100, -55);           // the room: about -55 dB
  expect(r.ev).toBeNull();
  r = run(vad, r.t, 1500, -25);                               // speech, well above the floor
  expect(r.ev).toBeNull();
  expect(vad.speaking).toBe(true);
  r = run(vad, r.t, 1000, -55);                               // a pause shorter than the end
  expect(r.ev).toBeNull();
  r = run(vad, r.t, 1000, -55);
  expect(r.ev).toBe('end');
});

it('a short pause mid-sentence does not end the turn', () => {
  const vad = new Vad();
  let r = run(vad, 0, 600, -55);
  r = run(vad, r.t, 800, -25);
  r = run(vad, r.t, 900, -55);                                // breath
  r = run(vad, r.t, 800, -25);                                // and on
  expect(r.ev).toBeNull();
});

it('trailing off between the two thresholds keeps listening', () => {
  const vad = new Vad();
  let r = run(vad, 0, 600, -55);
  r = run(vad, r.t, 600, -25);
  r = run(vad, r.t, 3000, -45);                               // 10 dB over the floor: neither speech nor quiet
  expect(r.ev).toBeNull();
});

it('a cough is not speech: too short to count', () => {
  const vad = new Vad();
  let r = run(vad, 0, 600, -55);
  r = run(vad, r.t, 200, -20);
  r = run(vad, r.t, 3000, -55);
  expect(r.ev).toBeNull();
  expect(vad.speaking).toBe(false);
});

it('nothing said for 8 s ends the conversation', () => {
  const vad = new Vad();
  const r = run(vad, 0, 10_000, -55);
  expect(r.ev).toBe('no_speech');
  expect(r.at).toBeGreaterThanOrEqual(VAD.noSpeechMs);
});

it('a noisy room raises the floor, so its noise is not taken for speech', () => {
  const vad = new Vad();
  let r = run(vad, 0, 600, -35);                              // a café
  r = run(vad, r.t, 2000, -30);                               // noise, under floor + 14
  expect(vad.speaking).toBe(false);
  r = run(vad, r.t, 800, -12);                                // him, close to the phone
  r = run(vad, r.t, 2000, -35);
  expect(r.ev).toBe('end');
});

it('the floor is bounded: a silent simulator still hears speech, and no reading counts as silence', () => {
  const vad = new Vad();
  let r = run(vad, 0, 600, -160);                             // floor clamps to -70
  r = run(vad, r.t, 600, -40);
  expect(vad.speaking).toBe(true);
  const missing = new Vad();
  for (let t = 0; t < 9000; t += 100) {
    const ev = missing.feed(null, t);
    if (ev) {
      expect(ev).toBe('no_speech');
      return;
    }
  }
  throw new Error('no event');
});

it('after a turn: waits for the answer and its voice, then listens again', () => {
  const base = { heard: 'what is NVDA at', speaker: 'idle' as const, spoke: false, sinceAnswered: 0 };
  expect(afterSend({ ...base, state: undefined })).toBe('wait');
  expect(afterSend({ ...base, state: 'running' })).toBe('wait');
  expect(afterSend({ ...base, state: 'answered', speaker: 'loading' })).toBe('wait');
  expect(afterSend({ ...base, state: 'answered', speaker: 'speaking', spoke: true })).toBe('wait');
  expect(afterSend({ ...base, state: 'answered', spoke: true })).toBe('listen');
  expect(afterSend({ ...base, state: 'answered', sinceAnswered: 100 })).toBe('wait');          // voice not started yet
  expect(afterSend({ ...base, state: 'answered', sinceAnswered: SPEECH_START_GRACE_MS })).toBe('listen');
  expect(afterSend({ ...base, state: 'absorbed', sinceAnswered: SPEECH_START_GRACE_MS })).toBe('listen');
});

it('a failed turn ends the conversation, and so does a goodbye once answered', () => {
  const base = { heard: 'x', speaker: 'idle' as const, spoke: true, sinceAnswered: 0 };
  for (const state of ['failed', 'interrupted', 'cancelled', 'send_failed', 'unknown']) {
    expect(afterSend({ ...base, state })).toBe('end');
  }
  expect(afterSend({ ...base, state: 'answered', heard: "Thanks, that's all." })).toBe('end');
  expect(afterSend({ ...base, state: 'answered', heard: 'thanks, that is all for now' })).toBe('end');
  expect(afterSend({ ...base, state: 'running', heard: "that's all" })).toBe('wait');   // the answer first
});

it('goodbyes in both languages, and nothing that merely contains the words', () => {
  for (const s of ["That's all", 'stop', 'Merci, c\'est tout.', 'goodbye Jarvis', 'OK thanks that is all', 'au revoir']) {
    expect(isGoodbye(s)).toBe(true);
  }
  for (const s of ['stop the gym block tomorrow', "that's all I know about NVDA, what do you think?", '', null]) {
    expect(isGoodbye(s)).toBe(false);
  }
});

it('the floor is clamped at both ends', () => {
  const silent = new Vad();
  let r = run(silent, 0, 600, -160);                          // a dead input: floor -70, not -160
  r = run(silent, r.t, 1000, -140);                           // still near nothing
  expect(silent.speaking).toBe(false);
  const loud = new Vad();
  r = run(loud, 0, 600, -10);                                 // clipping noise: floor -30, not -10
  r = run(loud, r.t, 600, -12);
  expect(loud.speaking).toBe(true);
});

it('quiet, then trailing off, then quiet again starts the quiet clock over', () => {
  const vad = new Vad();
  let r = run(vad, 0, 600, -55);
  r = run(vad, r.t, 600, -25);
  r = run(vad, r.t, 800, -55);                                // quiet
  r = run(vad, r.t, 1000, -45);                               // trailing off (between thresholds)
  r = run(vad, r.t, 600, -55);                                // quiet again: 600 ms, not 2.4 s
  expect(r.ev).toBeNull();
});

it('a long answer of his own past 8 s is not taken for silence', () => {
  const vad = new Vad();
  let r = run(vad, 0, 600, -55);
  r = run(vad, r.t, 9000, -25);
  expect(r.ev).toBeNull();
});

it('the floor is measured over half a second, not from the first sample alone', () => {
  const vad = new Vad();
  let r = run(vad, 0, 100, -20);                              // the tap that opened it
  r = run(vad, r.t, 600, -55);
  r = run(vad, r.t, 600, -25);
  expect(vad.speaking).toBe(true);
});

it('a goodbye must be the whole sentence', () => {
  expect(isGoodbye("I don't know when to stop")).toBe(false);
  expect(isGoodbye('and then say goodbye')).toBe(false);
});
