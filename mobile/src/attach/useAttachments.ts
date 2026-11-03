// The files waiting under the composer (slice 7). Each uploads as soon as it is picked, so Send
// only has to name them; a file the server refuses shows why, and can be removed.
import * as DocumentPicker from 'expo-document-picker';
import * as ImagePicker from 'expo-image-picker';
import { useCallback, useRef, useState } from 'react';

import { HttpError, uploadFile } from '../chat/client';
import { explain } from '../chat/useChat';
import type { ServerConfig } from '../core/api';
import { fromDocument, fromImage, refusal, room, type Pending } from './model';

export type Source = 'library' | 'camera' | 'files';

/** The picker for a source, as Pending files (empty when he cancelled). Photos come back as
 *  JPEG: iOS converts HEIC when asked for a compatible representation. A refused permission
 *  throws PermissionRefused, so the screen can point at Settings. */
export class PermissionRefused extends Error {}

export async function pick(source: Source, limit: number): Promise<Pending[]> {
  const imageOptions: ImagePicker.ImagePickerOptions = {
    mediaTypes: ['images'], quality: 0.85,
    preferredAssetRepresentationMode: ImagePicker.UIImagePickerPreferredAssetRepresentationMode.Compatible,
  };
  if (source === 'camera') {
    const perm = await ImagePicker.requestCameraPermissionsAsync();
    if (!perm.granted) throw new PermissionRefused('camera');
    const r = await ImagePicker.launchCameraAsync(imageOptions);
    return r.canceled ? [] : r.assets.slice(0, limit).map(fromImage);
  }
  if (source === 'library') {
    const r = await ImagePicker.launchImageLibraryAsync({ ...imageOptions, allowsMultipleSelection: limit > 1,
      selectionLimit: limit });
    return r.canceled ? [] : r.assets.slice(0, limit).map(fromImage);
  }
  const r = await DocumentPicker.getDocumentAsync({ multiple: limit > 1, copyToCacheDirectory: true });
  return r.canceled ? [] : r.assets.slice(0, limit).map(fromDocument);
}

export function useAttachments(cfg: ServerConfig | null, upload = uploadFile) {
  const [items, setItems] = useState<Pending[]>([]);
  const itemsRef = useRef(items);
  itemsRef.current = items;

  const patch = useCallback((id: string, change: Partial<Pending>) => {
    setItems((list) => list.map((p) => (p.id === id ? { ...p, ...change } : p)));
  }, []);

  const start = useCallback(async (p: Pending) => {
    const why = refusal(p);
    if (why) return patch(p.id, { state: 'failed', error: why });
    if (!cfg) return patch(p.id, { state: 'failed', error: 'Not paired.' });
    try {
      const up = await upload(cfg, p.uri, p.name, p.mimeType);
      patch(p.id, { state: 'ready', uploadId: up.upload_id, kind: up.kind });
    } catch (e) {
      // The server's own reason for a file it refused ("over 20 MB", "HEIC...") says it best.
      const said = e instanceof HttpError && e.status >= 400 && e.status < 500 && e.status !== 401;
      patch(p.id, { state: 'failed', error: said ? (e as HttpError).message : explain(e) });
    }
  }, [cfg, patch, upload]);

  /** Add picked files, up to the five a message can carry; each starts uploading at once. */
  const add = useCallback((picked: Pending[]) => {
    const take = picked.slice(0, room(itemsRef.current));
    if (!take.length) return;
    setItems((list) => [...list, ...take]);
    for (const p of take) void start(p);
  }, [start]);

  const remove = useCallback((id: string) => setItems((list) => list.filter((p) => p.id !== id)), []);
  const retry = useCallback((id: string) => {
    const p = itemsRef.current.find((x) => x.id === id);
    if (!p || p.state !== 'failed' || refusal(p)) return;
    patch(id, { state: 'uploading', error: undefined });
    void start(p);
  }, [patch, start]);
  const clear = useCallback(() => setItems([]), []);

  return { items, add, remove, retry, clear };
}
