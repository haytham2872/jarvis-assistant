import { IDLE, type SpeakState } from './speechQueue';
import { createLevelBus, orbMode, playbackLevel, rms } from './levels';

describe('level bus', () => {
  it('passes levels to every subscriber, clamped to 0..1', () => {
    let t = 0;
    const bus = createLevelBus(50, () => t);
    const a: number[] = [], b: number[] = [];
    bus.subscribe((v) => a.push(v));
    const off = bus.subscribe((v) => b.push(v));
    bus.push(0.4); t += 60; bus.push(3); t += 60; bus.push(NaN);
    off();
    t += 60; bus.push(0.2);
    expect(a).toEqual([0.4, 1, 0, 0.2]);
    expect(b).toEqual([0.4, 1, 0]);
  });

  it('keeps at most one level per interval, but silence always gets through', () => {
    let t = 0;
    const bus = createLevelBus(50, () => t);
    const got: number[] = [];
    bus.subscribe((v) => got.push(v));
    bus.push(0.5); t += 10; bus.push(0.6); t += 10; bus.push(0); t += 60; bus.push(0.7);
    expect(got).toEqual([0.5, 0, 0.7]);
  });
});

describe('playback level', () => {
  it('is the RMS of the first channel, stretched for speech', () => {
    expect(rms([])).toBe(0);
    expect(rms([0.5, -0.5])).toBeCloseTo(0.5);
    expect(playbackLevel({ channels: [{ frames: [0.1, -0.1, 0.1, -0.1] }], timestamp: 0 })).toBeCloseTo(0.4);
    expect(playbackLevel({ channels: [{ frames: [0.9, -0.9] }], timestamp: 0 })).toBe(1);
    expect(playbackLevel({ channels: [], timestamp: 0 })).toBe(0);
  });
});

describe('orb mode', () => {
  const speaking: SpeakState = { source: 'req:1', phase: 'speaking', voice: 'server' };
  const loading: SpeakState = { source: 'req:1', phase: 'loading' };
  it('puts recording first, then speaking, transcribing, thinking', () => {
    expect(orbMode({ recording: true, speech: speaking, states: ['running'] })).toBe('listening');
    expect(orbMode({ recording: false, speech: speaking, states: ['transcribing'] })).toBe('speaking');
    expect(orbMode({ recording: false, speech: IDLE, states: ['running', 'transcribing'] })).toBe('transcribing');
    expect(orbMode({ recording: false, speech: loading, states: [] })).toBe('thinking');
    for (const s of ['running', 'queued', 'sending'] as const) {
      expect(orbMode({ recording: false, speech: IDLE, states: [s] })).toBe('thinking');
    }
  });
  it('is idle when nothing is under way', () => {
    expect(orbMode({ recording: false, speech: IDLE, states: ['answered', 'failed', 'send_failed', 'interrupted'] })).toBe('idle');
  });
});
