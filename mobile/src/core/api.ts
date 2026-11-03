// Addressing JARVIS's /mobile/v1 routes (backend/mobile/routes.py): the base address, the
// bearer header, and what a failure means in words. Pure, so tested without a phone.

export type ServerConfig = { baseUrl: string; token: string };

/** "host:8443/" -> "https://host:8443"; keeps an explicit http:// for the dev server. */
export function normalizeBase(raw: string): string {
  let url = raw.trim().replace(/\/+$/, '');
  if (!url) return '';
  if (!/^https?:\/\//i.test(url)) url = `https://${url}`;
  return url;
}

/** Every route is <base>/mobile/v1/... : the dev server serves it, and the server's
 * `tailscale serve --set-path /mobile` mapping keeps the prefix (DEPLOY.md). */
export function endpoint(base: string, path: string): string {
  const b = normalizeBase(base);
  // An empty base would make every request relative, which on a phone means Metro (the
  // server that delivered the app): 14 October, ping parsed Metro's web page as JSON.
  if (!b) throw new ExplainedError('Not paired yet: tap "Scan pairing QR" first.');
  return `${b}/mobile/v1${path}`;
}

/** A failure whose message is already the explanation (shown as it is). */
export class ExplainedError extends Error {}

export function wsUrl(httpUrl: string): string {
  return httpUrl.replace(/^http/i, 'ws');
}

export function authHeaders(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token.trim()}` };
}

/** What a failure means for the owner, in words. */
export function describeFailure(status: number | null, detail = ''): string {
  if (status === null) {
    // 14 October on 5G: iOS reported "A server with the specified hostname could not be
    // found" for the tailnet name while the server itself answered; the phone was not
    // resolving .ts.net names.
    if (/hostname could not be found|could not resolve|ENOTFOUND|getaddrinfo/i.test(detail)) {
      return "The phone can't find the server's name. Open the Tailscale app: it must say Connected, " +
        'with "Use Tailscale DNS settings" on.';
    }
    return `Can't reach JARVIS. Is Tailscale (or the Wi-Fi) on, and the address right? ${detail}`.trim();
  }
  if (status === 401) return 'JARVIS refused the token. Check it matches MOBILE_TOKEN on the server.';
  if (status === 404) return 'The phone API is off on that server (MOBILE_TOKEN not set), or the address is wrong.';
  if (status === 413) return `Too large for JARVIS. ${detail}`.trim();
  if (status === 415) return `JARVIS could not decode that audio. ${detail}`.trim();
  if (status === 502) return `The voice service failed. ${detail}`.trim();
  return `JARVIS answered ${status}. ${detail}`.trim();
}

/** iOS metering is in dBFS (-160..0). Speech sits around -50..-10; map that to 0..1. */
export function meterLevel(db: number | undefined | null): number {
  if (db === undefined || db === null || !Number.isFinite(db)) return 0;
  return Math.max(0, Math.min(1, (db + 55) / 50));
}
