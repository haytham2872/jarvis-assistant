// A pairing QR for the app's own scanner ("Scan pairing QR"), so the address and token
// never have to be typed on the phone:
//
//   MOBILE_TOKEN=... node mobile/tools/pair_qr.mjs --base https://<server>.ts.net [--svg out.svg]
//
// It carries jarvis-pair:?base=…&token=… (src/core/pairing.ts). Scanned inside
// the app, not with the iPhone camera: on 14 October a camera-scanned exp:// deep link
// never reached the open app. The token goes from the screen into the phone's keychain;
// it is never requested over the network.
import { writeFileSync } from 'node:fs';
import { toQR } from 'toqr';

const args = Object.fromEntries(
  process.argv.slice(2).reduce((acc, a, i, all) => (a.startsWith('--') ? [...acc, [a.slice(2), all[i + 1]]] : acc), []),
);
const token = (process.env.MOBILE_TOKEN || '').trim();
if (!args.base || !token) {
  console.error('usage: MOBILE_TOKEN=... node pair_qr.mjs --base URL [--svg out.svg]');
  process.exit(1);
}

const params = new URLSearchParams({ base: args.base, token });
const payload = `jarvis-pair:?${params}`;

const cells = toQR(payload);
const n = Math.round(Math.sqrt(cells.length));
const dark = (x, y) => x >= 0 && y >= 0 && x < n && y < n && cells[y * n + x] === 1;

// Terminal: two rows per character, with a quiet zone.
const lines = [];
for (let y = -2; y < n + 2; y += 2) {
  let line = '';
  for (let x = -2; x < n + 2; x++) {
    const top = dark(x, y);
    const bottom = dark(x, y + 1);
    line += top && bottom ? ' ' : top ? '▄' : bottom ? '▀' : '█';
  }
  lines.push(line);
}
console.log(lines.join('\n'));
console.log(`\nIn the app: SERVER › Scan pairing QR. Pairs with ${args.base}`);

if (args.svg) {
  const s = 10;
  const rects = [];
  for (let y = 0; y < n; y++) for (let x = 0; x < n; x++) if (dark(x, y)) rects.push(`<rect x="${(x + 4) * s}" y="${(y + 4) * s}" width="${s}" height="${s}"/>`);
  const size = (n + 8) * s;
  writeFileSync(args.svg, `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}"><rect width="100%" height="100%" fill="#fff"/><g fill="#000">${rects.join('')}</g></svg>`);
  console.log(`QR image: ${args.svg}`);
}
