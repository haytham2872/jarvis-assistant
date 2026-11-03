import { chatWhen } from './when';

it('reads a chat time the way a phone does', () => {
  const now = new Date(2026, 9, 15, 22, 0);
  const at = (y: number, m: number, d: number, h = 9, min = 5) => new Date(y, m, d, h, min).getTime() / 1000;
  expect(chatWhen(at(2026, 9, 15, 14, 2), now)).toBe('14:02');
  expect(chatWhen(at(2026, 9, 14), now)).toBe('Yesterday');
  expect(chatWhen(at(2026, 9, 12), now)).toBe('Mon 12 Oct');
  expect(chatWhen(at(2025, 11, 31), now)).toBe('Wed 31 Dec 2025');
});
