// Speaking one answer (docs/archive/MOBILE-APP-PLAN.md M3): the server's plan of clips, played in order
// with at most two fetched ahead, the iPhone's own voice for whatever the server's voice
// cannot give. Pure orchestration: the audio, the network and the iPhone voice are passed
// in, so it is tested without a phone.
//
// Stop is immediate and local: it bumps a generation, halts the player and the iPhone
// voice, and nothing already in flight can start playing afterwards. Nothing is cancelled
// on the server.

export type Lang = 'en' | 'fr';
export type Clip = { seq: number; text: string; lang: Lang };
export type SpeechPlan = { source: string; revision: string; summarised: boolean; clips: Clip[] };
export type Ended = 'done' | 'stopped';

export type SpeechDeps = {
  plan(source: string): Promise<SpeechPlan>;
  /** A local file holding the clip's audio. */
  fetchClip(source: string, revision: string, seq: number): Promise<string>;
  play(uri: string): Promise<Ended>;
  speakLocal(text: string, lang: Lang): Promise<Ended>;
  /** Stop the player and the iPhone voice now; their pending play/speak resolve 'stopped'. */
  halt(): void;
};

export type SpeakState = {
  source: string | null;
  phase: 'idle' | 'loading' | 'speaking';
  voice?: 'server' | 'phone';
};

export const IDLE: SpeakState = { source: null, phase: 'idle' };
export const PREFETCH_AHEAD = 2;

const FRENCH = /[éèêàçùâîôœ]|\b(le|la|les|une|est|et|je|tu|ton|ta|pour|avec|dans|pas|que|qui)\b/i;

export function guessLang(text: string): Lang {
  const fr = (text.match(new RegExp(FRENCH.source, 'gi')) ?? []).length;
  const en = (text.match(/\b(the|and|is|you|your|to|of|it|in|for|with)\b/gi) ?? []).length;
  return fr > en ? 'fr' : 'en';
}

/** What the iPhone voice reads when not even the plan could be had: the visible text,
 * without the Markdown symbols. */
export function plainText(markdown: string): string {
  return markdown
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/^\s*·\s.*$/gm, '')
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/https?:\/\/\S+/g, 'a link')
    .replace(/[*_`#>|]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

export class SpeechQueue {
  private gen = 0;

  constructor(private deps: SpeechDeps, private onState: (s: SpeakState) => void) {}

  stop(): void {
    this.gen++;
    this.deps.halt();
    this.onState(IDLE);
  }

  async speak(source: string, fallbackText = ''): Promise<Ended | 'failed'> {
    this.deps.halt();
    const gen = ++this.gen;
    const live = () => gen === this.gen;
    this.onState({ source, phase: 'loading' });

    let plan: SpeechPlan;
    try {
      plan = await this.deps.plan(source);
    } catch {
      if (!live()) return 'stopped';
      const text = plainText(fallbackText);
      if (!text) {
        this.onState(IDLE);
        return 'failed';
      }
      this.onState({ source, phase: 'speaking', voice: 'phone' });
      return this.end(gen, await this.deps.speakLocal(text, guessLang(text)));
    }
    if (!live()) return 'stopped';

    const fetches = new Map<number, Promise<string>>();
    const ensure = (seq: number) => {
      if (seq >= plan.clips.length || fetches.has(seq)) return;
      const p = this.deps.fetchClip(source, plan.revision, seq);
      p.catch(() => undefined);              // awaited (or abandoned) later; never unhandled
      fetches.set(seq, p);
    };

    let serverVoice = true;
    for (const clip of plan.clips) {
      if (!live()) return 'stopped';
      let ended: Ended | null = null;
      if (serverVoice) {
        for (let k = clip.seq; k <= clip.seq + PREFETCH_AHEAD; k++) ensure(k);
        try {
          const uri = await fetches.get(clip.seq)!;
          if (!live()) return 'stopped';
          this.onState({ source, phase: 'speaking', voice: 'server' });
          ended = await this.deps.play(uri);
        } catch {
          if (!live()) return 'stopped';
          serverVoice = false;   // the rest is read by the iPhone: no gaps waiting on a failing server
        }
      }
      if (ended === null) {
        this.onState({ source, phase: 'speaking', voice: 'phone' });
        ended = await this.deps.speakLocal(clip.text, clip.lang);
      }
      if (ended === 'stopped' || !live()) return 'stopped';
    }
    return this.end(gen, 'done');
  }

  private end(gen: number, ended: Ended): Ended {
    if (gen === this.gen) this.onState(IDLE);
    return ended;
  }
}
