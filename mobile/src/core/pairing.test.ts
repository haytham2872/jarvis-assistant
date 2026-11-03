import { PAIR_PREFIX, parsePairing, tokenHint } from './pairing';

const TOKEN = 'dev-' + 'x'.repeat(40);
const code = (q: string) => `${PAIR_PREFIX}?${q}`;

describe('pairing code', () => {
  it('reads the address and token the PC put in it', () => {
    const r = parsePairing(code(`base=http%3A%2F%2F192.168.0.26%3A8791%2F&token=${TOKEN}`));
    expect(r).toEqual({ ok: true, settings: { baseUrl: 'http://192.168.0.26:8791', token: TOKEN } });
  });

  it('ignores the relay an older code carried', () => {
    const r = parsePairing(code(`base=https%3A%2F%2Fjarvis.tail1.ts.net&token=${TOKEN}&relay=http%3A%2F%2F10.0.0.2%3A8793`));
    expect(r).toEqual({ ok: true, settings: { baseUrl: 'https://jarvis.tail1.ts.net', token: TOKEN } });
  });

  it.each([
    ['any other QR', 'https://example.com/menu'],
    ["another app's code with the same fields", `otherapp-qr:?base=https%3A%2F%2Fh.ts.net&token=${TOKEN}`],
    ['the old exp:// deep link', `exp://192.168.0.26:8081/--/pair?base=http%3A%2F%2Fh&token=${TOKEN}`],
    ['no address', code(`token=${TOKEN}`)],
    ['an address that is not a URL', code(`base=jarvis&token=${TOKEN}`)],
    ['a short token', code('base=https%3A%2F%2Fh.ts.net&token=short')],
  ])('refuses %s', (_label, data) => {
    expect(parsePairing(data).ok).toBe(false);
  });

  it('shows only the last four characters of a token', () => {
    expect(tokenHint(TOKEN)).toBe('…xxxx');
    expect(tokenHint('')).toBe('none');
    expect(tokenHint('abc')).toBe('…');
  });
});
