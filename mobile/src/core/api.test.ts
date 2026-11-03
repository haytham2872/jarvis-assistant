import { authHeaders, describeFailure, endpoint, meterLevel, normalizeBase, wsUrl } from './api';

describe('addresses', () => {
  it('adds https and drops trailing slashes', () => {
    expect(normalizeBase(' instance.tail1234.ts.net/ ')).toBe('https://instance.tail1234.ts.net');
    expect(normalizeBase('http://192.168.0.26:8791//')).toBe('http://192.168.0.26:8791');
    expect(normalizeBase('')).toBe('');
  });

  it('always puts routes under /mobile/v1', () => {
    expect(endpoint('https://host.ts.net', '/ping')).toBe('https://host.ts.net/mobile/v1/ping');
    expect(endpoint('http://192.168.0.26:8791/', '/events')).toBe('http://192.168.0.26:8791/mobile/v1/events');
  });

  it('turns http(s) into ws(s) for the socket', () => {
    expect(wsUrl('https://h/mobile/v1/events')).toBe('wss://h/mobile/v1/events');
    expect(wsUrl('http://h/mobile/v1/events')).toBe('ws://h/mobile/v1/events');
  });

  it('sends the token as a bearer header', () => {
    expect(authHeaders(' abc ')).toEqual({ Authorization: 'Bearer abc' });
  });
});

describe('failures in words', () => {
  it('names the likely cause for each refusal', () => {
    expect(describeFailure(null)).toMatch(/Tailscale/);
    // The exact iOS message seen on 5G, 14 October.
    expect(describeFailure(null, 'fetch failed: UnexpectedException: A server with the specified hostname could not be found.'))
      .toMatch(/can't find the server's name.*Use Tailscale DNS/);
    expect(describeFailure(401)).toMatch(/token/);
    expect(describeFailure(404)).toMatch(/MOBILE_TOKEN/);
    expect(describeFailure(502, 'edge-tts failed')).toMatch(/voice service.*edge-tts/);
  });
});

describe('meter', () => {
  it('maps speech-range dBFS onto 0..1 and clamps', () => {
    expect(meterLevel(-160)).toBe(0);
    expect(meterLevel(0)).toBe(1);
    expect(meterLevel(-30)).toBeCloseTo(0.5);
    expect(meterLevel(undefined)).toBe(0);
    expect(meterLevel(Number.NaN)).toBe(0);
  });
});

describe('never a relative request', () => {
  afterEach(() => jest.restoreAllMocks());

  it('refuses to build an address without a paired server', () => {
    expect(() => endpoint('', '/ping')).toThrow(/Not paired/);
  });
});
