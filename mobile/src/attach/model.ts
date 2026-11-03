// Photos and documents from the phone (slice 7), pure. What a picked file becomes before it is
// uploaded, what is refused before wasting an upload, and how a message's file line (the server
// adds "[Sent image: attachments/…]" to his words, as on Discord) is shown as chips.

export type AttachKind = 'image' | 'document';
export type AttachState = 'uploading' | 'ready' | 'failed';

export type Pending = {
  id: string;                 // this phone's
  uri: string;
  name: string;
  mimeType: string;
  size: number | null;
  kind: AttachKind;
  state: AttachState;
  uploadId?: string;
  error?: string;
};

export type FileChip = { kind: AttachKind; name: string; path?: string };

export const MAX_ATTACH = 5;
export const MAX_IMAGE_BYTES = 20 * 2 ** 20;
export const MAX_DOCUMENT_BYTES = 25 * 2 ** 20;
const DOCUMENT_EXT = new Set(['pdf', 'docx', 'xlsx', 'pptx', 'csv', 'txt', 'md', 'json', 'html', 'htm']);
const IMAGE_EXT = new Set(['jpg', 'jpeg', 'png', 'webp', 'gif', 'bmp', 'heic', 'heif']);

const ext = (name: string) => (name.match(/\.([A-Za-z0-9]+)$/)?.[1] ?? '').toLowerCase();

let seq = 0;
const localId = () => `att-${Date.now().toString(36)}-${(seq++).toString(36)}`;

/** A photo from the library or the camera. iOS hands back JPEG when asked for a compatible
 *  representation; a HEIC name is renamed to match what is actually sent. */
export function fromImage(a: { uri: string; fileName?: string | null; mimeType?: string | null; fileSize?: number | null }): Pending {
  const mimeType = a.mimeType || 'image/jpeg';
  let name = a.fileName || `photo-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-')}.jpg`;
  if (mimeType === 'image/jpeg' && !['jpg', 'jpeg'].includes(ext(name))) name = name.replace(/\.[^.]*$/, '') + '.jpg';
  return { id: localId(), uri: a.uri, name, mimeType, size: a.fileSize ?? null, kind: 'image', state: 'uploading' };
}

/** A file from the Files app. */
export function fromDocument(a: { uri: string; name: string; mimeType?: string | null; size?: number | null }): Pending {
  const kind: AttachKind = IMAGE_EXT.has(ext(a.name)) || (a.mimeType ?? '').startsWith('image/') ? 'image' : 'document';
  return { id: localId(), uri: a.uri, name: a.name, mimeType: a.mimeType || 'application/octet-stream',
    size: a.size ?? null, kind, state: 'uploading' };
}

/** Why a file can't be sent, before uploading it; null when it can. */
export function refusal(p: Pick<Pending, 'name' | 'kind' | 'size'>): string | null {
  const e = ext(p.name);
  if (p.kind === 'document' && !DOCUMENT_EXT.has(e)) return `JARVIS can't read .${e || '?'} files`;
  if (p.size != null && p.size > (p.kind === 'image' ? MAX_IMAGE_BYTES : MAX_DOCUMENT_BYTES)) {
    return p.kind === 'image' ? 'over 20 MB' : 'over 25 MB';
  }
  return null;
}

/** Room left for more files in this message. */
export const room = (items: Pending[]) => Math.max(0, MAX_ATTACH - items.length);

export function sendable(items: Pending[]): { ready: Pending[]; busy: boolean } {
  return { ready: items.filter((p) => p.state === 'ready'), busy: items.some((p) => p.state === 'uploading') };
}

const MARKER = /\n*\[Sent ((?:(?:image|document): [^,\]]+(?:, )?)+)\]\s*$/;

/** A file's name as he would recognise it: no folder, no date stamp, no note extension. */
export function chipName(path: string): string {
  const base = path.replace(/^.*\//, '').replace(/\.md$/, '');
  return base.replace(/^\d{4}-\d{2}-\d{2}-\d{4}-/, '').replace(/-\d+$/, '') || base;
}

/** His words without the file line, and the files it named. */
export function splitMarker(text: string): { text: string; files: FileChip[] } {
  const m = (text ?? '').match(MARKER);
  if (!m) return { text: text ?? '', files: [] };
  const files = m[1].split(', ').map((part) => {
    const [kind, path] = part.split(': ');
    return { kind: (kind === 'image' ? 'image' : 'document') as AttachKind, path, name: chipName(path) };
  });
  return { text: (text ?? '').slice(0, m.index).trimEnd(), files };
}
