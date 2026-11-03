// The tray's files: each uploads as soon as it is picked, and says why when it can't.
import { act, renderHook, waitFor } from '@testing-library/react-native';
import * as DocumentPicker from 'expo-document-picker';
import * as ImagePicker from 'expo-image-picker';

import { HttpError } from '../chat/client';
import { fromDocument, fromImage, type Pending } from './model';
import { pick, PermissionRefused, useAttachments } from './useAttachments';

jest.mock('expo-image-picker', () => ({
  requestCameraPermissionsAsync: jest.fn(), launchCameraAsync: jest.fn(), launchImageLibraryAsync: jest.fn(),
  UIImagePickerPreferredAssetRepresentationMode: { Compatible: 'compatible' },
}));
jest.mock('expo-document-picker', () => ({ getDocumentAsync: jest.fn() }));

const cfg = { baseUrl: 'https://jarvis.example/mobile', token: 't' };
const photo = (name = 'IMG_1.jpg') => fromImage({ uri: `file:///${name}`, fileName: name, mimeType: 'image/jpeg', fileSize: 1000 });

async function hook(upload: jest.Mock) {
  return await renderHook(() => useAttachments(cfg, upload));
}
const states = (items: Pending[]) => items.map((p) => [p.name, p.state, p.uploadId ?? p.error ?? null]);

it('a picked photo uploads at once and is ready with its upload id', async () => {
  // Held open until the state has been seen: an awaited act (Testing Library 14) would
  // otherwise let an instant upload finish first.
  let done: (v: unknown) => void = () => undefined;
  const upload = jest.fn(() => new Promise((resolve) => { done = resolve; }));
  const h = await hook(upload);
  await act(() => h.result.current.add([photo()]));
  expect(h.result.current.items[0].state).toBe('uploading');
  await act(async () => done({ upload_id: 'up-1', name: 'IMG_1.jpg', kind: 'image', bytes: 1000 }));
  await waitFor(() => expect(states(h.result.current.items)).toEqual([['IMG_1.jpg', 'ready', 'up-1']]));
  expect(upload).toHaveBeenCalledWith(cfg, 'file:///IMG_1.jpg', 'IMG_1.jpg', 'image/jpeg');
});

it("a file the phone already knows can't be read is not uploaded", async () => {
  const upload = jest.fn();
  const h = await hook(upload);
  await act(() => h.result.current.add([fromDocument({ uri: 'u', name: 'tool.exe', size: 10 })]));
  await waitFor(() => expect(h.result.current.items[0].state).toBe('failed'));
  expect(h.result.current.items[0].error).toMatch(/\.exe/);
  expect(upload).not.toHaveBeenCalled();
});

it("the server's own reason for refusing a file is shown; a network failure is explained", async () => {
  const upload = jest.fn()
    .mockRejectedValueOnce(new HttpError(415, "I can't read .xyz files"))
    .mockRejectedValueOnce(new TypeError('Network request failed'));
  const h = await hook(upload);
  await act(() => h.result.current.add([photo('a.jpg'), photo('b.jpg')]));
  await waitFor(() => expect(h.result.current.items.every((p) => p.state === 'failed')).toBe(true));
  expect(h.result.current.items[0].error).toBe("I can't read .xyz files");
  expect(h.result.current.items[1].error).not.toBe('Network request failed');   // in words, not the raw error
  expect(h.result.current.items[1].error).toBeTruthy();
});

it('a failed upload is retried, removed, and the tray cleared after sending', async () => {
  const upload = jest.fn().mockRejectedValueOnce(new TypeError('offline'))
    .mockResolvedValue({ upload_id: 'up-2', name: 'x', kind: 'image', bytes: 1 });
  const h = await hook(upload);
  await act(() => h.result.current.add([photo()]));
  await waitFor(() => expect(h.result.current.items[0].state).toBe('failed'));
  await act(() => h.result.current.retry(h.result.current.items[0].id));
  await waitFor(() => expect(h.result.current.items[0].state).toBe('ready'));
  await act(() => h.result.current.add([photo('second.jpg')]));
  await waitFor(() => expect(h.result.current.items).toHaveLength(2));
  await act(() => h.result.current.remove(h.result.current.items[0].id));
  expect(h.result.current.items.map((p) => p.name)).toEqual(['second.jpg']);
  await act(() => h.result.current.clear());
  expect(h.result.current.items).toEqual([]);
});

it('no more than five files a message', async () => {
  const upload = jest.fn().mockResolvedValue({ upload_id: 'up', name: 'x', kind: 'image', bytes: 1 });
  const h = await hook(upload);
  await act(() => h.result.current.add([1, 2, 3, 4, 5, 6, 7].map((n) => photo(`${n}.jpg`))));
  await waitFor(() => expect(h.result.current.items.every((p) => p.state === 'ready')).toBe(true));
  expect(h.result.current.items).toHaveLength(5);
  expect(upload).toHaveBeenCalledTimes(5);
});

describe('pick', () => {
  const lib = ImagePicker.launchImageLibraryAsync as jest.Mock;
  beforeEach(() => jest.clearAllMocks());

  it('asks Photos for JPEG, several at once up to the room left', async () => {
    lib.mockResolvedValue({ canceled: false, assets: [
      { uri: 'file:///1', fileName: 'IMG_1.HEIC', mimeType: 'image/jpeg', fileSize: 5 },
      { uri: 'file:///2', fileName: 'IMG_2.HEIC', mimeType: 'image/jpeg', fileSize: 5 },
    ] });
    const got = await pick('library', 1);
    expect(lib).toHaveBeenCalledWith(expect.objectContaining({
      preferredAssetRepresentationMode: 'compatible', allowsMultipleSelection: false, selectionLimit: 1 }));
    expect(got.map((p) => p.name)).toEqual(['IMG_1.jpg']);
  });

  it('cancelling picks nothing', async () => {
    lib.mockResolvedValue({ canceled: true, assets: null });
    expect(await pick('library', 5)).toEqual([]);
  });

  it('a refused camera says so', async () => {
    (ImagePicker.requestCameraPermissionsAsync as jest.Mock).mockResolvedValue({ granted: false });
    await expect(pick('camera', 5)).rejects.toBeInstanceOf(PermissionRefused);
    expect(ImagePicker.launchCameraAsync).not.toHaveBeenCalled();
  });

  it('files come from the Files app', async () => {
    (DocumentPicker.getDocumentAsync as jest.Mock).mockResolvedValue({ canceled: false, assets: [
      { uri: 'file:///lease.pdf', name: 'lease.pdf', mimeType: 'application/pdf', size: 900 }] });
    const got = await pick('files', 5);
    expect(DocumentPicker.getDocumentAsync).toHaveBeenCalledWith({ multiple: true, copyToCacheDirectory: true });
    expect(got).toEqual([expect.objectContaining({ name: 'lease.pdf', kind: 'document', size: 900 })]);
  });
});

it('only a failed file is retried, and a refused one never uploads', async () => {
  const upload = jest.fn().mockResolvedValue({ upload_id: 'up-9', name: 'x', kind: 'image', bytes: 1 });
  const h = await hook(upload);
  await act(() => h.result.current.add([photo(), fromDocument({ uri: 'u', name: 'tool.exe', size: 1 })]));
  await waitFor(() => expect(h.result.current.items.map((p) => p.state)).toEqual(['ready', 'failed']));
  await act(() => {
    h.result.current.retry(h.result.current.items[0].id);
    h.result.current.retry(h.result.current.items[1].id);
  });
  expect(h.result.current.items.map((p) => p.state)).toEqual(['ready', 'failed']);
  expect(upload).toHaveBeenCalledTimes(1);
});
