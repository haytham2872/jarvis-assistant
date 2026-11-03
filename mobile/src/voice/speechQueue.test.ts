import { guessLang, IDLE, plainText, PREFETCH_AHEAD, SpeechQueue, type SpeakState, type SpeechDeps, type SpeechPlan } from './speechQueue';

const tick = () => new Promise((r) => setTimeout(r, 0));

const PLAN: SpeechPlan = {
  source: 'req:r1', revision: 'rev', summarised: false,
  clips: [
    { seq: 0, text: 'Bonjour.', lang: 'fr' },
    { seq: 1, text: 'Your call is at ten.', lang: 'en' },
    { seq: 2, text: 'Bring the lease.', lang: 'en' },
    { seq: 3, text: 'That is all.', lang: 'en' },
    { seq: 4, text: 'Bye.', lang: 'en' },
  ],
};

/** Fakes that record everything; play() waits until the test lets the clip finish. */
function fakes(over: Partial<SpeechDeps> = {}) {
  const log: string[] = [];
  let finishPlay: ((e: 'done' | 'stopped') => void) | null = null;
  let finishLocal: ((e: 'done' | 'stopped') => void) | null = null;
  let finished = 0;                    // clips played to the end
  const deps: SpeechDeps = {
    plan: jest.fn(async () => PLAN),
    fetchClip: jest.fn(async (_s, _r, seq) => {
      log.push(`fetch ${seq} after ${finished}`);
      return `file-${seq}`;
    }),
    play: jest.fn((uri: string) => new Promise((resolve) => {
      log.push(`play ${uri}`);
      finishPlay = (e) => {
        if (e === 'done') finished++;
        resolve(e);
      };
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
    ...over,
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
  return { q, deps, log, states, finish };
}

it('plays every clip in order, fetching at most two ahead of the one playing', async () => {
  const { q, log, states, finish } = fakes();
  const done = q.speak('req:r1');
  for (let i = 0; i < PLAN.clips.length; i++) await finish();
  expect(await done).toBe('done');
  expect(log.filter((l) => l.startsWith('play'))).toEqual(['play file-0', 'play file-1', 'play file-2', 'play file-3', 'play file-4']);
  // While clip i is next to play (i clips finished), nothing past i + 2 has been asked for.
  for (const line of log.filter((l) => l.startsWith('fetch'))) {
    const [, seq, , finished] = line.split(' ');
    expect(Number(seq) - Number(finished)).toBeLessThanOrEqual(PREFETCH_AHEAD);
  }
  expect(states[0]).toEqual({ source: 'req:r1', phase: 'loading' });
  expect(states.at(-1)).toEqual(IDLE);
});

it('stop is immediate: nothing else plays or is read afterwards', async () => {
  const { q, deps, log, states, finish } = fakes();
  const done = q.speak('req:r1');
  await finish();                         // clip 0 finished, clip 1 playing
  await tick();
  q.stop();
  expect(await done).toBe('stopped');
  await finish();
  expect(log.filter((l) => l.startsWith('play'))).toEqual(['play file-0', 'play file-1']);
  expect(deps.speakLocal).not.toHaveBeenCalled();
  expect(states.at(-1)).toEqual(IDLE);
});

it('a stop while the plan or a clip is still loading plays nothing when it arrives', async () => {
  let releasePlan: (p: SpeechPlan) => void = () => undefined;
  const slow = fakes({ plan: jest.fn(() => new Promise<SpeechPlan>((r) => { releasePlan = r; })) });
  const done = slow.q.speak('req:r1');
  slow.q.stop();
  releasePlan(PLAN);
  expect(await done).toBe('stopped');
  expect(slow.deps.play).not.toHaveBeenCalled();

  const releases: ((u: string) => void)[] = [];
  const slowClip = fakes({
    fetchClip: jest.fn((_s, _r, seq: number) => new Promise<string>((r) => { releases[seq] = r; })),
  });
  const done2 = slowClip.q.speak('req:r1');
  await tick();
  slowClip.q.stop();
  releases.forEach((r, seq) => r(`file-${seq}`));
  expect(await done2).toBe('stopped');
  expect(slowClip.deps.play).not.toHaveBeenCalled();
});

it('reading another answer stops the one being read', async () => {
  const { q, log, finish } = fakes();
  const first = q.speak('req:r1');
  await tick();
  const second = q.speak('ex:x2');
  expect(await first).toBe('stopped');
  for (let i = 0; i < PLAN.clips.length; i++) await finish();
  expect(await second).toBe('done');
  expect(log.filter((l) => l.startsWith('play')).length).toBe(1 + PLAN.clips.length);
});

it("a clip the server cannot give is read by the iPhone, and so is the rest", async () => {
  const fetchClip = jest.fn(async (_s: string, _r: string, seq: number) => {
    if (seq >= 1) throw new Error('HTTP 503 monthly allowance used');
    return `file-${seq}`;
  });
  const { q, log, states, finish } = fakes({ fetchClip });
  const done = q.speak('req:r1');
  for (let i = 0; i < PLAN.clips.length; i++) await finish();
  expect(await done).toBe('done');
  expect(log.filter((l) => !l.startsWith('fetch'))).toEqual([
    'play file-0',
    'local en Your call is at ten.', 'local en Bring the lease.', 'local en That is all.', 'local en Bye.',
  ]);
  expect(states.some((s) => s.voice === 'phone')).toBe(true);
});

it('when not even the plan comes, the iPhone reads the visible text; with none, nothing', async () => {
  const noPlan = { plan: jest.fn(async () => { throw new Error('offline'); }) };
  const a = fakes(noPlan);
  const done = a.q.speak('req:r1', 'Ton **rendez-vous** est jeudi. [Lien](https://x.fr)');
  await a.finish();
  expect(await done).toBe('done');
  expect(a.log).toEqual(['local fr Ton rendez-vous est jeudi. Lien']);

  const b = fakes(noPlan);
  expect(await b.q.speak('req:r1', '')).toBe('failed');
  expect(b.states.at(-1)).toEqual(IDLE);
});

it('plain text and language guesses', () => {
  expect(plainText('## Plan\n```\ncode\n```\n- **gym** at 6 · ok\n· followed: x')).toBe('Plan - gym at 6 · ok');
  expect(guessLang("J'ai vérifié ton agenda pour demain")).toBe('fr');
  expect(guessLang('Your call is at ten and you have the lease')).toBe('en');
});

it('after one failed clip the iPhone keeps reading: no switching back and forth between voices', async () => {
  const fetchClip = jest.fn(async (_s: string, _r: string, seq: number) => {
    if (seq === 1) throw new Error('HTTP 502');
    return `file-${seq}`;
  });
  const { q, log, finish } = fakes({ fetchClip });
  const done = q.speak('req:r1');
  for (let i = 0; i < PLAN.clips.length; i++) await finish();
  expect(await done).toBe('done');
  expect(log.filter((l) => !l.startsWith('fetch'))).toEqual([
    'play file-0',
    'local en Your call is at ten.', 'local en Bring the lease.', 'local en That is all.', 'local en Bye.',
  ]);
});
