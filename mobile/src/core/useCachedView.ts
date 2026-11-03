import { Directory, File, Paths } from 'expo-file-system';
import { useFocusEffect } from 'expo-router';
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';

import type { Conditional } from '../chat/client';
import { normalizeBase, type ServerConfig } from './api';
import { mark, since } from './perf';

type Entry<T> = { data: T; updatedAt: number; etag?: string };
const memory = new Map<string, Entry<unknown>>();
export function clearViewMemory() { memory.clear(); }

// Two independent 32-bit hashes keep filenames short and keep credentials out of the file.
// The complete scope is also checked inside each file before its data can be used.
function hash(text: string) {
  let a = 2166136261, b = 5381;
  for (let i = 0; i < text.length; i++) {
    a = Math.imul(a ^ text.charCodeAt(i), 16777619);
    b = Math.imul(b, 33) ^ text.charCodeAt(i);
  }
  return `${(a >>> 0).toString(16)}${(b >>> 0).toString(16)}`;
}

export function viewKey(cfg: ServerConfig | null, name: string): string | null {
  return cfg ? `${normalizeBase(cfg.baseUrl)}|${hash(cfg.token.trim())}|${name}` : null;
}

function fileFor(key: string) {
  const directory = new Directory(Paths.document, 'views');
  return { directory, file: new File(directory, `${hash(key)}.json`) };
}

async function read<T>(key: string): Promise<Entry<T> | undefined> {
  const hit = memory.get(key) as Entry<T> | undefined;
  if (hit) return hit;
  try {
    const { file } = fileFor(key);
    if (!file.exists) return;
    const saved = JSON.parse(await file.text());
    if (saved.key !== key || saved.data === undefined || !Number.isFinite(saved.updatedAt)) return;
    // A slow file read must not replace a newer network response.
    if (!memory.has(key)) memory.set(key, { data: saved.data, updatedAt: saved.updatedAt, etag: saved.etag });
    return memory.get(key) as Entry<T>;
  } catch { /* unreadable cache: fetch normally */ }
}

function save<T>(key: string, entry: Entry<T>) {
  memory.delete(key);
  memory.set(key, entry);
  if (memory.size > 24) memory.delete(memory.keys().next().value!);
  try {
    const { directory, file } = fileFor(key);
    directory.create({ intermediates: true, idempotent: true });
    file.write(JSON.stringify({ key, ...entry }));
  } catch { /* a cache failure never hides the server's answer */ }
}

export function staleLabel(updatedAt: number | null, refreshMs: number, now = Date.now()): string {
  if (updatedAt === null || now - updatedAt <= refreshMs) return '';
  const minutes = Math.floor((now - updatedAt) / 60_000);
  return minutes < 1 ? 'Updated less than a minute ago' : `Updated ${minutes} min ago`;
}

/** Show the last view immediately, then validate it on focus and while this tab is open.
 * The optional conditional argument also works with servers that don't yet send ETags. */
export function useCachedView<T>(key: string | null, fetcher: (cache: Conditional<T>) => Promise<T>,
  { refreshMs }: { refreshMs: number }) {
  const [state, setState] = useState<{ key: string | null; entry?: Entry<T>; error?: unknown; loaded: boolean }>(() => ({
    key, entry: key ? memory.get(key) as Entry<T> | undefined : undefined, loaded: false,
  }));
  const fetchRef = useRef(fetcher);
  const activeKey = useRef(key);
  useLayoutEffect(() => {
    fetchRef.current = fetcher;
    activeKey.current = key;
  }, [fetcher, key]);
  const revision = useRef(0);
  const inFlight = useRef<{ key: string; promise: Promise<void> } | null>(null);
  const alive = useRef(true);
  const first = useRef<string | null>(null);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  const show = useCallback((entry: Entry<T> | undefined, error?: unknown, loaded = true) => {
    if (alive.current && activeKey.current === key) setState({ key, entry, error, loaded });
  }, [key]);

  const reload = useCallback((force = false): Promise<void> => {
    if (!key) return Promise.resolve();
    if (!force && inFlight.current?.key === key) return inFlight.current.promise;
    if (force) revision.current++;
    const version = revision.current;
    const fetch = fetchRef.current;
    const promise = (async () => {
      const cached = await read<T>(key);
      if (cached) show(cached, undefined, false);
      let etag = cached?.etag;
      try {
        const data = await fetch({ etag, cached: cached?.data, onEtag: (value) => { etag = value; } });
        if (!alive.current || activeKey.current !== key || revision.current !== version) return;
        const entry = { data, etag, updatedAt: Date.now() };
        save(key, entry);
        show(entry);
      } catch (error) {
        if (revision.current === version) show(memory.get(key) as Entry<T> | undefined, error);
      }
    })().finally(() => {
      if (inFlight.current?.promise === promise) inFlight.current = null;
    });
    inFlight.current = { key, promise };
    return promise;
  }, [key, show]);

  useFocusEffect(useCallback(() => {
    void reload();
    const timer = setInterval(() => void reload(), refreshMs);
    return () => clearInterval(timer);
  }, [reload, refreshMs]));

  const setData = useCallback((data: T) => {
    if (!key) return;
    revision.current++;
    const entry = { data, updatedAt: Date.now() };  // mutation invalidates the old ETag
    save(key, entry);
    show(entry);
  }, [key, show]);

  const entry = state.key === key ? state.entry : key ? memory.get(key) as Entry<T> | undefined : undefined;
  useEffect(() => {
    if (!entry || !key || first.current === key) return;
    first.current = key;
    const name = `tab:${key.split('|').pop()!.split(':')[0]}:first-data`;
    mark(name);
    since('app:start', name);
  }, [entry, key]);
  return {
    data: entry?.data ?? null, error: state.key === key ? state.error : undefined,
    updatedAt: entry?.updatedAt ?? null, reload, setData, loaded: state.key === key && state.loaded,
    stale: staleLabel(entry?.updatedAt ?? null, refreshMs),
  };
}
