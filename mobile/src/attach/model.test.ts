import { chipName, fromDocument, fromImage, refusal, room, sendable, splitMarker, type Pending } from './model';

it('a photo is sent as JPEG and named so', () => {
  const p = fromImage({ uri: 'file:///a', fileName: 'IMG_0042.HEIC', mimeType: 'image/jpeg', fileSize: 1000 });
  expect(p).toMatchObject({ name: 'IMG_0042.jpg', mimeType: 'image/jpeg', kind: 'image', state: 'uploading', size: 1000 });
  expect(fromImage({ uri: 'file:///b' }).name).toMatch(/^photo-.*\.jpg$/);
  expect(fromImage({ uri: 'file:///c', fileName: 'shot.png', mimeType: 'image/png' }).name).toBe('shot.png');
});

it('a file from Files is a document unless it is a picture', () => {
  expect(fromDocument({ uri: 'u', name: 'lease.pdf', mimeType: 'application/pdf' }).kind).toBe('document');
  expect(fromDocument({ uri: 'u', name: 'scan.png' }).kind).toBe('image');
  expect(fromDocument({ uri: 'u', name: 'x', mimeType: 'image/jpeg' }).kind).toBe('image');
});

it('refuses before uploading what the server could not read', () => {
  expect(refusal({ name: 'a.heic', kind: 'image', size: 10 })).toBeNull();      // the server reads HEIC
  expect(fromDocument({ uri: 'u', name: 'IMG_1.HEIC' }).kind).toBe('image');
  expect(refusal({ name: 'tool.exe', kind: 'document', size: 10 })).toMatch(/\.exe/);
  expect(refusal({ name: 'big.jpg', kind: 'image', size: 21 * 2 ** 20 })).toBe('over 20 MB');
  expect(refusal({ name: 'big.pdf', kind: 'document', size: 24 * 2 ** 20 })).toBeNull();
  expect(refusal({ name: 'huge.pdf', kind: 'document', size: 26 * 2 ** 20 })).toBe('over 25 MB');
  expect(refusal({ name: 'ok.docx', kind: 'document', size: null })).toBeNull();
});

it('at most five, and nothing sends while one is still uploading', () => {
  const p = (state: Pending['state']) => ({ state } as Pending);
  expect(room([p('ready'), p('ready')])).toBe(3);
  expect(room(Array(7).fill(p('ready')))).toBe(0);
  expect(sendable([p('ready'), p('failed')])).toEqual({ ready: [p('ready')], busy: false });
  expect(sendable([p('ready'), p('uploading')]).busy).toBe(true);
});

it('the file line is shown as chips, his words without it', () => {
  const s = splitMarker('what is the total?\n\n[Sent image: attachments/2026-10-17-1432-receipt.md, document: attachments/2026-10-17-1432-lease-2.md]');
  expect(s.text).toBe('what is the total?');
  expect(s.files).toEqual([
    { kind: 'image', path: 'attachments/2026-10-17-1432-receipt.md', name: 'receipt' },
    { kind: 'document', path: 'attachments/2026-10-17-1432-lease-2.md', name: 'lease' },
  ]);
  expect(splitMarker('[Sent image: attachments/2026-10-17-0900-photo.md]')).toEqual(
    { text: '', files: [{ kind: 'image', path: 'attachments/2026-10-17-0900-photo.md', name: 'photo' }] });
  expect(splitMarker('just words [Sent nothing]').files).toEqual([]);
  expect(splitMarker('[Sent image: x] and then more').files).toEqual([]);   // only at the end
});

it('chip names drop folder, stamp and note extension', () => {
  expect(chipName('attachments/2026-10-17-1432-bank-statement.md')).toBe('bank-statement');
  expect(chipName('attachments/plain.pdf')).toBe('plain.pdf');
});

it("the share Shortcut's address names the file \"shared\" and ends with q=", () => {
  const { shareAddress } = jest.requireActual('./share');
  expect(shareAddress('http://100.1.2.3:8765')).toBe('http://100.1.2.3:8765/mobile/v1/ask?name=shared&q=');
});
