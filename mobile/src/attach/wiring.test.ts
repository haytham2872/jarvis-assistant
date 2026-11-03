/**
 * @jest-environment node
 */
// Where attachments are used (slice 7). The chat screen has no screen test of its own, so its
// wiring is checked in its source: the composer gets the tray and the paperclip, Send names the
// uploads of the ready files only, and both chat screens turn the file line into chips.
import fs from 'fs';
import path from 'path';

const src = (rel: string) => fs.readFileSync(path.join(__dirname, '..', rel), 'utf8');

it('the chat screen wires the tray, the paperclip and the uploads into Send', () => {
  const s = src('app/(tabs)/index.tsx');
  expect(s).toMatch(/attachments=\{attach\.items\} onAttach=\{onAttach\}/);
  expect(s).toContain('const { ready, busy } = sendable(attach.items);');
  expect(s).toContain('if (busy || (!text && !ready.length)) return;');
  expect(s).toContain('uploads: ready.map((p) => p.uploadId!)');
  expect(s).toContain('attach.clear();');
  expect(s).toContain('splitMarker(raw)');
  expect(s).toContain('files={r.files}');
});

it('past chats show the files as chips too', () => {
  const s = src('app/chat/[id].tsx');
  expect(s).toContain('<FileChips files={splitMarker(item.user).files} />');
});
