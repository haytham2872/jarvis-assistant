import { fireEvent, render, screen } from '@testing-library/react-native';
import { Linking } from 'react-native';
import { lexer } from 'marked';

import { Markdown, openLink } from './Markdown';

jest.mock('marked', () => {
  const actual = jest.requireActual('marked');
  return { ...actual, lexer: jest.fn(actual.lexer) };
});

beforeEach(() => { jest.clearAllMocks(); jest.spyOn(Linking, 'openURL').mockResolvedValue(undefined); });
afterEach(() => jest.restoreAllMocks());

it('draws bold, italics and inline code without their symbols', async () => {
  await render(<Markdown text={'Your **NVDA** is *up* and `AAPL` flat.'} />);
  expect(screen.getByText('NVDA')).toBeTruthy();
  expect(screen.getByText('up')).toBeTruthy();
  expect(screen.getByText('AAPL')).toBeTruthy();
  expect(screen.queryByText(/\*\*/)).toBeNull();
});

it('draws headings, lists, quotes and code blocks', async () => {
  await render(<Markdown text={'## Plan\n\n- gym at 6\n- call Samir\n\n1. first\n2. second\n\n> quoted\n\n```py\nprint(1)\n```'} />);
  expect(screen.getByRole('header')).toBeTruthy();
  expect(screen.getByText('Plan')).toBeTruthy();
  expect(screen.getAllByText('•')).toHaveLength(2);
  expect(screen.getByText('2.')).toBeTruthy();
  expect(screen.getByText('quoted')).toBeTruthy();
  expect(screen.getByText('print(1)')).toBeTruthy();
});

it('draws a table cell by cell', async () => {
  await render(<Markdown text={'| Ticker | P&L |\n|---|---|\n| NVDA | +$1,240 |'} />);
  for (const cell of ['Ticker', 'P&L', 'NVDA', '+$1,240']) expect(screen.getByText(cell)).toBeTruthy();
});

it('opens web links in Safari and nothing else', async () => {
  await render(<Markdown text={'See [Reuters](https://reuters.com/x).'} />);
  await fireEvent.press(screen.getByText('Reuters'));
  expect(Linking.openURL).toHaveBeenCalledWith('https://reuters.com/x');
  expect(openLink('javascript:alert(1)')).toBe(false);
  expect(openLink('file:///etc/passwd')).toBe(false);
  expect(openLink('mailto:a@b.c')).toBe(true);
});

it('shows an unfinished draft rather than nothing', async () => {
  await render(<Markdown text={'Looking it up:\n```\npartial'} streaming />);
  expect(screen.getByText('Looking it up:')).toBeTruthy();
  expect(screen.getByText('partial')).toBeTruthy();
});

it('parses drafts without retaining them, then caches the same text when it becomes final', async () => {
  const text = 'A streaming **answer** becoming final.';
  const { rerender, unmount } = await render(<Markdown text={text} streaming />);
  expect(screen.getByText('answer')).toBeTruthy();
  expect(lexer).toHaveBeenCalledTimes(1);
  await rerender(<Markdown text={text} />);
  expect(lexer).toHaveBeenCalledTimes(2);
  await unmount();
  await render(<Markdown text={text} />);
  expect(lexer).toHaveBeenCalledTimes(2);
});

it('opens goal evidence inside the app without forwarding its private scheme to Safari', async () => {
  const evidence = jest.fn();
  await render(<Markdown text="Fee [ev123](jarvis-evidence:ev123)." onEvidence={evidence} />);
  await fireEvent.press(screen.getByText('ev123'));
  expect(evidence).toHaveBeenCalledWith('ev123');
  expect(Linking.openURL).not.toHaveBeenCalled();
});
