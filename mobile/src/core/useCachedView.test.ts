import { act, renderHook, waitFor } from '@testing-library/react-native';
import { clearViewMemory, staleLabel, useCachedView, viewKey } from './useCachedView';
import type { Conditional } from '../chat/client';

const mockFiles = new Map<string, string>();
jest.mock('expo-file-system', () => ({
  Paths: { document: 'file:///documents' },
  Directory: class {
    uri: string;
    constructor(...parts: (string | { uri: string })[]) { this.uri = parts.map((p) => typeof p === 'string' ? p : p.uri).join('/'); }
    create() {}
  },
  File: class {
    uri: string;
    constructor(...parts: (string | { uri: string })[]) { this.uri = parts.map((p) => typeof p === 'string' ? p : p.uri).join('/'); }
    get exists() { return mockFiles.has(this.uri); }
    async text() { return mockFiles.get(this.uri); }
    write(text: string) { mockFiles.set(this.uri, text); }
  },
}));
jest.mock('expo-router', () => ({
  useFocusEffect: (fn: () => void | (() => void)) => jest.requireActual('react').useEffect(fn, [fn]),
}));

const options = { refreshMs: 60_000 };
const key = viewKey({ baseUrl: 'https://j', token: 'secret' }, 'today')!;
beforeEach(() => { clearViewMemory(); mockFiles.clear(); });

it('shows a memory hit synchronously while refreshing, then replaces and persists it', async () => {
  const first = await renderHook(() => useCachedView(key, async () => ({ count: 1 }), options));
  await waitFor(() => expect(first.result.current.data).toEqual({ count: 1 }));
  await first.unmount();
  let finish!: (v: { count: number }) => void;
  const fetch = jest.fn(() => new Promise<{ count: number }>((resolve) => { finish = resolve; }));
  const next = await renderHook(() => useCachedView(key, fetch, options));
  expect(next.result.current.data).toEqual({ count: 1 });
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  await act(async () => finish({ count: 2 }));
  expect(next.result.current.data).toEqual({ count: 2 });
  expect(JSON.parse([...mockFiles.values()][0]).data).toEqual({ count: 2 });
  expect([...mockFiles.values()][0]).not.toContain('secret');
});

it('reads the file after a cold start and keeps it through an offline refresh', async () => {
  const seed = await renderHook(() => useCachedView(key, async () => ({ saved: true }), options));
  await waitFor(() => expect(seed.result.current.data).toEqual({ saved: true }));
  await seed.unmount();
  clearViewMemory();
  const fetch = jest.fn().mockRejectedValue(new Error('Offline'));
  const cold = await renderHook(() => useCachedView(key, fetch, options));
  await waitFor(() => expect(cold.result.current.error).toBeInstanceOf(Error));
  expect(cold.result.current.data).toEqual({ saved: true });
  expect(fetch.mock.calls[0][0].cached).toEqual({ saved: true });
});

it('carries the ETag through file persistence and does not duplicate an in-flight refresh', async () => {
  const fetch = jest.fn(async (cache) => { cache.onEtag('W/"one"'); return { value: 1 }; });
  const h = await renderHook(() => useCachedView(key, fetch, options));
  await waitFor(() => expect(h.result.current.data).toEqual({ value: 1 }));
  await h.unmount(); clearViewMemory();
  let finish!: (v: { value: number }) => void;
  const refresh = jest.fn((_cache: Conditional<{ value: number }>) => new Promise<{ value: number }>((resolve) => { finish = resolve; }));
  const cold = await renderHook(() => useCachedView(key, refresh, options));
  await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
  expect(refresh.mock.calls[0][0]).toMatchObject({ etag: 'W/"one"', cached: { value: 1 } });
  void cold.result.current.reload();
  expect(refresh).toHaveBeenCalledTimes(1);
  await act(async () => finish({ value: 2 }));
});

it('does not show another server or account, even if its old request finishes late', async () => {
  let finish!: (v: { private: string }) => void;
  const slow = jest.fn(() => new Promise<{ private: string }>((resolve) => { finish = resolve; }));
  const other = viewKey({ baseUrl: 'https://other', token: 'secret' }, 'today')!;
  const h = await renderHook(({ scope }: { scope: string }) => useCachedView(scope, slow, options), { initialProps: { scope: key } });
  await waitFor(() => expect(slow).toHaveBeenCalledTimes(1));
  const oldFinish = finish;
  await h.rerender({ scope: other });
  expect(h.result.current.data).toBeNull();
  await waitFor(() => expect(slow).toHaveBeenCalledTimes(2));
  await act(async () => oldFinish({ private: 'old server' }));
  expect(h.result.current.data).toBeNull();
  expect(viewKey({ baseUrl: 'https://j', token: 'different' }, 'today')).not.toBe(key);
  expect(viewKey({ baseUrl: 'https://j/', token: 'secret' }, 'today')).toBe(key);
});

it('a successful tap invalidates the validator and wins over an older refresh', async () => {
  let finish!: (v: { count: number }) => void;
  const fetch = jest.fn(() => new Promise<{ count: number }>((resolve) => { finish = resolve; }));
  const h = await renderHook(() => useCachedView(key, fetch, options));
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  await act(() => h.result.current.setData({ count: 4 }));
  await act(async () => finish({ count: 1 }));
  expect(h.result.current.data).toEqual({ count: 4 });
  expect(JSON.parse([...mockFiles.values()][0]).etag).toBeUndefined();
});

it('labels only data older than its refresh interval', () => {
  expect(staleLabel(0, 60_000, 60_000)).toBe('');
  expect(staleLabel(0, 60_000, 180_000)).toBe('Updated 3 min ago');
  expect(staleLabel(null, 60_000, 180_000)).toBe('');
});

it('a forced reload after an approval does not join an older GET', async () => {
  const finish: ((v: { count: number }) => void)[] = [];
  const fetch = jest.fn(() => new Promise<{ count: number }>((resolve) => { finish.push(resolve); }));
  const h = await renderHook(() => useCachedView(key, fetch, options));
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  void h.result.current.reload(true);
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  await act(async () => finish[1]({ count: 2 }));
  await act(async () => finish[0]({ count: 1 }));
  expect(h.result.current.data).toEqual({ count: 2 });
});
