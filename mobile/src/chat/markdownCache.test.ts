import { lexer } from 'marked';
import { markdownTokens } from './markdownCache';

jest.mock('marked', () => ({ lexer: jest.fn((text: string) => [{ type: 'paragraph', text }]) }));

it('reuses exact text, keeps recently used tokens, and evicts the oldest at 200', () => {
  const first = markdownTokens('first');
  expect(markdownTokens('first')).toBe(first);
  for (let n = 0; n < 199; n++) markdownTokens(`text-${n}`);
  expect(markdownTokens('first')).toBe(first); // renew it
  markdownTokens('one more');
  expect(markdownTokens('first')).toBe(first);
  const count = (lexer as jest.Mock).mock.calls.length;
  markdownTokens('text-0');
  expect((lexer as jest.Mock).mock.calls.length).toBe(count + 1);
});

it('keeps a finished answer cached across 300 streaming drafts without caching the drafts', () => {
  const finished = 'A finished answer retained while the next answer streams.';
  const saved = markdownTokens(finished);
  let lastDraft;
  for (let n = 1; n <= 300; n++) lastDraft = markdownTokens('draft '.repeat(n), { keep: false });
  const calls = (lexer as jest.Mock).mock.calls.length;
  expect(markdownTokens(finished)).toBe(saved);
  expect((lexer as jest.Mock).mock.calls.length).toBe(calls);
  expect(markdownTokens('draft '.repeat(300))).not.toBe(lastDraft);
  expect((lexer as jest.Mock).mock.calls.length).toBe(calls + 1);
});
