// Live speech: clips announced while JARVIS is still writing, played in order as they arrive.
import { IDLE, SpeechQueue, type SpeakState, type SpeechDeps } from './speechQueue';

const tick = () => new Promise((r) => setTimeout(r, 0));

function fakes(fetchLive?: SpeechDeps['fetchLive']) {
  const log: string[] = [];
  let finishPlay: ((e: 'done' | 'stopped') => void) | null = null;
  let finishLocal: ((e: 'done' | 'stopped') => void) | null = null;
  const deps: SpeechDeps = {
    plan: jest.fn(async () => { throw new Error('plan must not be used'); }),
    fetchClip: jest.fn(async () => 'x'),
    fetchLive: jest.fn(fetchLive ?? (async (_s: string, seq: number) => `live-${seq}`)),
    play: jest.fn((uri: string) => new Promise((resolve) => {
      log.push(`play ${uri}`);
      finishPlay = resolve;
    })),
    speakLocal: jest.fn((text: string, lang) => new Promise((resolve) => {
      log.push(`local ${lang} ${text}`);
      finishLocal = resolve;
    })),
    halt: jest.fn(() => {
      finishPlay?.('stopped');
      finishLocal?.('stopped');
      finishPlay = finishLocal = null;
    }),
  };
  const states: SpeakState[] = [];
  const q = new SpeechQueue(deps, (s) => states.push(s));
  const finish = async () => {
    await tick();
    const f = finishPlay ?? finishLocal;
    finishPlay = finishLocal = null;
    f?.('done');
    await tick();
  };
  const clip = (seq: number, text = `Clip ${seq}.`) => ({ type: 'speech_clip', clip: seq, text, lang: 'en' as const });
  const end = (clips: number) => ({ type: 'speech_end', clips });
  return { q, deps, log, states, finish, clip, end };
}
const last = (s: SpeakState[]) => s[s.length - 1];

it('plays clips in the order they arrive, prefetching the next while one plays', async () => {
  const { q, deps, log, states, finish, clip, end } = fakes();
  q.pushLive('req:r1', clip(0));
  await tick();
  expect(last(states)).toEqual({ source: 'req:r1', phase: 'speaking', voice: 'server' });
  expect(log).toContain('play live-0');
  q.pushLive('req:r1', clip(1));
  await tick();
  expect(deps.fetchLive).toHaveBeenCalledWith('req:r1', 1);     // fetched while clip 0 still plays
  expect(log).not.toContain('play live-1');
  await finish();
  expect(log).toContain('play live-1');
  q.pushLive('req:r1', end(2));
  await finish();
  expect(last(states)).toEqual(IDLE);
  expect(deps.plan).not.toHaveBeenCalled();
});

it('waits for a clip not yet arrived (loading) and plays out-of-order arrivals in order', async () => {
  const { q, log, states, finish, clip, end } = fakes();
  q.pushLive('req:r1', clip(1));
  q.pushLive('req:r1', clip(0));
  await tick();
  expect(log.filter((l) => l.startsWith('play'))).toEqual(['play live-0']);
  await finish();
  expect(log.filter((l) => l.startsWith('play'))).toEqual(['play live-0', 'play live-1']);
  await finish();                                               // clip 1 done, clip 2 not here yet
  expect(last(states)).toEqual({ source: 'req:r1', phase: 'loading' });
  q.pushLive('req:r1', clip(2));
  await tick();
  expect(last(states).phase).toBe('speaking');
  await finish();
  expect(last(states).phase).toBe('loading');                   // no speech_end yet: not idle
  q.pushLive('req:r1', end(3));
  await tick();
  expect(last(states)).toEqual(IDLE);
});

it('the first state is loading, and idle needs speech_end AND every clip played', async () => {
  const { q, states, finish, clip, end } = fakes();
  q.pushLive('req:r1', end(1));                                 // end before the clip: not idle yet
  q.pushLive('req:r1', clip(0));
  expect(states[0]).toEqual({ source: 'req:r1', phase: 'loading' });
  await tick();
  expect(last(states).phase).toBe('speaking');
  await finish();
  expect(last(states)).toEqual(IDLE);
});

it('a clip the server voice cannot give (503 or 502) is read by the phone voice, and so is the rest as on the plan path', async () => {
  const { q, log, finish, clip, end } = fakes(async (_s, seq) => {
    if (seq === 0) throw Object.assign(new Error('allowance'), { status: 503 });
    return `live-${seq}`;
  });
  q.pushLive('req:r1', clip(0, 'Hello there.'));
  q.pushLive('req:r1', clip(1));
  q.pushLive('req:r1', end(2));
  await tick();
  expect(log).toContain('local en Hello there.');
  await finish();
  expect(log).toContain('local en Clip 1.');
});

it('a clip the server does not have (404) is skipped', async () => {
  const { q, log, states, clip, end } = fakes(async () => { throw Object.assign(new Error('gone'), { status: 404 }); });
  q.pushLive('req:r1', clip(0));
  q.pushLive('req:r1', end(1));
  await tick();
  await tick();
  expect(log).toEqual([]);
  expect(last(states)).toEqual(IDLE);
});

it('stop ends it at once and later clips of that source are ignored', async () => {
  const { q, deps, log, states, clip } = fakes();
  q.pushLive('req:r1', clip(0));
  await tick();
  q.stop();
  expect(deps.halt).toHaveBeenCalled();
  expect(last(states)).toEqual(IDLE);
  const before = states.length;
  q.pushLive('req:r1', clip(1));
  await tick();
  expect(log).not.toContain('play live-1');
  expect(states.length).toBe(before);
  q.pushLive('req:r2', clip(0));                                // a new turn is fine
  await tick();
  expect(log.filter((l) => l === 'play live-0')).toHaveLength(2);
});

it('a new source replaces the one being spoken', async () => {
  const { q, log, finish, clip } = fakes();
  q.pushLive('req:r1', clip(0));
  await tick();
  q.pushLive('req:r2', clip(0));
  await tick();
  q.pushLive('req:r1', clip(1));                                // the old turn's clips no longer count
  await tick();
  await finish();
  expect(log.filter((l) => l.startsWith('play'))).toEqual(['play live-0', 'play live-0']);
});
