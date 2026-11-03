/**
 * @jest-environment node
 */
// Where the orb is used (slice 6). The chat screen has no screen test of its own, so its header
// orb's wiring is checked in its source: tinted by his mood, the GPU orb not the old one, and
// silent while conversation mode's own orb is on screen (only one orb buzzes).
import fs from 'fs';
import path from 'path';

const src = (rel: string) => fs.readFileSync(path.join(__dirname, '..', rel), 'utf8');

it('the chat header shows the neural orb as a badge, with his mood, quiet under conversation mode', () => {
  const s = src('app/(tabs)/index.tsx');
  const tag = s.match(/<NeuralOrb [^>]*\/>/)?.[0] ?? '';
  expect(tag).toContain('size={HEADER_ORB}');
  expect(tag).toContain('mood={state.mood}');
  expect(tag).toContain('haptics={!talking}');
  expect(s).toMatch(/const HEADER_ORB = (\d+);/);
  expect(Number(s.match(/const HEADER_ORB = (\d+);/)![1])).toBeLessThan(80);   // a badge: no motes
  expect(s).not.toMatch(/<Orb /);
});
