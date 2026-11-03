import { describeUpdate } from './version';

it('names the running update by when it was published', () => {
  const local = new Date(2026, 9, 15, 21, 40);           // 15 Oct 21:40 on the phone's clock
  expect(describeUpdate({ id: '01a13cc9-91a4-7b93', createdAt: local.toISOString() }, false))
    .toBe('Update published 15 Oct, 21:40 (01a13cc9)');
  expect(describeUpdate(null, false)).toBe('Update unknown');
  expect(describeUpdate({ createdAt: 'nonsense' }, false)).toBe('Update unknown');
  expect(describeUpdate({ createdAt: local.toISOString() }, true)).toBe('Development build from the PC');
});
