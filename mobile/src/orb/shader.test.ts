/**
 * @jest-environment node
 */
// The orb's shader compiled and drawn for real, by CanvasKit (Skia's WebAssembly build, the same
// SkSL compiler the phone uses), so an edit that breaks it fails here and not on the phone.
import path from 'path';

import { ORB_SKSL } from './shader';
import { LOOK, sizing } from './states';
import { pack, uniformsFor } from './uniforms';

type CK = any;                                   // canvaskit-wasm's own types are not needed here
let CK: CK;

beforeAll(async () => {
  const dir = path.dirname(require.resolve('canvaskit-wasm/bin/full/canvaskit.js'));
  // jest-expo installs Expo's TextDecoder, which knows only UTF-8; CanvasKit needs Node's.
  const g = globalThis as { TextDecoder: unknown };
  const expoDecoder = g.TextDecoder;
  g.TextDecoder = require('util').TextDecoder;
  try {
    CK = await require('canvaskit-wasm/bin/full/canvaskit.js')({ locateFile: (f: string) => path.join(dir, f) });
  } finally {
    g.TextDecoder = expoDecoder;
  }
}, 60_000);

/** The uniforms in the shader's declaration order, as CanvasKit takes them. */
function flat(u: ReturnType<typeof uniformsFor>): number[] {
  return [...u.resolution, u.time, u.amp, u.phase, ...u.colorA, ...u.colorB, u.level, u.glow, u.motes, u.radius];
}

function draw(size: number, mode: keyof typeof LOOK, level = 0, motes = true): Uint8Array {
  const eff = CK.RuntimeEffect.Make(ORB_SKSL);
  const z = sizing(size);
  const shader = eff.makeShader(flat(uniformsFor(pack(LOOK[mode], motes ? z.motes : 0), {
    size, time: 3, phase: 3 * LOOK[mode].speed, level, radius: z.radius, reduced: false,
  })));
  const surf = CK.MakeSurface(size, size);
  const paint = new CK.Paint();
  paint.setShader(shader);
  surf.getCanvas().clear(CK.TRANSPARENT);
  surf.getCanvas().drawRect(CK.XYWHRect(0, 0, size, size), paint);
  const px = surf.getCanvas().readPixels(0, 0, { width: size, height: size, colorType: CK.ColorType.RGBA_8888,
    alphaType: CK.AlphaType.Unpremul, colorSpace: CK.ColorSpace.SRGB });
  paint.delete(); shader.delete(); surf.delete(); eff.delete();
  return px;
}
const at = (px: Uint8Array, size: number, x: number, y: number) => px.slice((y * size + x) * 4, (y * size + x) * 4 + 4);

it('compiles, with exactly the uniforms the app sends', () => {
  let error = '';
  const eff = CK.RuntimeEffect.Make(ORB_SKSL, (e: string) => { error = e; });
  expect(error).toBe('');
  expect(eff).toBeTruthy();
  const names = Array.from({ length: eff.getUniformCount() }, (_, i) => eff.getUniformName(i));
  const sent = Object.keys(uniformsFor(pack(LOOK.idle), { size: 1, time: 0, phase: 0, level: 0, radius: 0.6, reduced: false }));
  expect(names.sort()).toEqual(sent.sort());
  eff.delete();
});

it('draws a solid, state-coloured body, a halo, and clear corners', () => {
  const size = 96;
  const px = draw(size, 'idle');
  const centre = at(px, size, 48, 48);
  expect(centre[3]).toBeGreaterThan(150);                                     // the body is there
  expect(centre[2]).toBeGreaterThan(centre[0]);                               // and blue at rest
  expect(at(px, size, 0, 0)[3]).toBeLessThan(10);                             // nothing in the corners
  const halo = at(px, size, 48 + 32, 48);                                     // just outside the body
  expect(halo[3]).toBeGreaterThan(0);
});

it('each state reads as its own colour', () => {
  const size = 64;
  const c = (m: keyof typeof LOOK) => at(draw(size, m), size, 18, 32);       // inside, near the rim
  expect(c('listening')[0]).toBeGreaterThan(c('listening')[2]);               // amber
  expect(c('thinking')[2]).toBeGreaterThan(c('thinking')[1]);                 // violet
  expect(c('idle')[2]).toBeGreaterThan(c('idle')[0]);                         // blue
});

/** Mean alpha over a ring of the box, radii as a share of its half-width. */
function ring(px: Uint8Array, size: number, r0: number, r1: number): number {
  let sum = 0;
  let n = 0;
  const h = size / 2;
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const r = Math.hypot(x + 0.5 - h, y + 0.5 - h) / h;
      if (r >= r0 && r < r1) { sum += px[(y * size + x) * 4 + 3]; n++; }
    }
  }
  return sum / n;
}

it('the voice brightens the halo, not only widens it', () => {
  // Just outside the rim: the voice makes it several times brighter (about 7x measured; about
  // 2x if only the halo's reach followed the voice).
  const quiet = ring(draw(96, 'speaking', 0, false), 96, 0.72, 0.8);   // motes off: they sit in this ring
  const loud = ring(draw(96, 'speaking', 1, false), 96, 0.72, 0.8);
  expect(loud).toBeGreaterThan(quiet * 4);
});

it('a badge-sized orb fills its box: the body reaches where a big one has only halo', () => {
  expect(ring(draw(40, 'idle'), 40, 0.66, 0.7)).toBeGreaterThan(120);      // radius 0.74: inside the body
  expect(ring(draw(96, 'idle'), 96, 0.66, 0.7)).toBeLessThan(60);          // radius 0.6: outside it
});
