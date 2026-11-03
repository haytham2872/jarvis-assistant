/// <reference types="node" />
// Expo Router bundles every file under src/app as a route: a test there is shipped in the app
// and pulls the testing library into the bundle (the 15 October publish failed on exactly that).
import { readdirSync, statSync } from 'fs';
import { join } from 'path';

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((n) => (statSync(join(dir, n)).isDirectory() ? files(join(dir, n)) : [join(dir, n)]));
}

it('keeps tests out of the routes folder', () => {
  expect(files(join(__dirname, '..', 'app')).filter((f) => /\.test\.[jt]sx?$/.test(f))).toEqual([]);
});
