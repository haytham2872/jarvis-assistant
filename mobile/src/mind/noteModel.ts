import type { Token, Tokens } from 'marked';
import { markdownTokens } from '../chat/markdownCache';

/** Parse once for reference links, then virtualize blocks, long code and wide tables.
 * Notes don't occupy the chat's finished-answer LRU. Plain-text fallback is also chunked. */
export function noteBlocks(text: string): Token[] {
  text = text.replace(/^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/, '');
  const tokens = markdownTokens(text, { keep: false });
  return splitBlocks(tokens ?? [{ type: 'paragraph', raw: text, text } as Token]);
}

function splitBlocks(tokens: Token[]): Token[] {
  const out: Token[] = [];
  for (const t of tokens) {
    if (t.type === 'space' || t.type === 'def' || (t.type === 'html' && /^\s*<!--[\s\S]*-->\s*$/.test(t.raw))) continue;
    if (t.type === 'blockquote') {
      const quote = t as Tokens.Blockquote;
      for (const child of splitBlocks(quote.tokens)) out.push({ ...quote, tokens: [child] });
    } else if (t.type === 'list') {
      const list = t as Tokens.List;
      for (let i = 0; i < list.items.length; i += 12) out.push({ ...list, start: typeof list.start === 'number' ? list.start + i : list.start, items: list.items.slice(i, i + 12) });
    } else if (t.type === 'table') {
      const table = t as Tokens.Table;
      for (let i = 0; i < Math.max(table.rows.length, 1); i += 12) out.push({ ...table, rows: table.rows.slice(i, i + 12) });
    } else if (t.type === 'code' && t.text.length > 2400) {
      for (const part of chunks(t.text)) out.push({ ...t, text: part });
    } else if (t.type === 'paragraph' && t.raw.length > 2400) {
      // A very long single paragraph must not become one huge native Text layout.
      const p = t as Tokens.Paragraph;
      for (const part of chunks(p.text)) {
        out.push({ type: 'paragraph', raw: part, text: part, tokens: [{ type: 'text', raw: part, text: part }] });
      }
    } else out.push(t);
  }
  return out;
}

function chunks(text: string) { return text.match(/[\s\S]{1,2400}/gu) ?? []; }

export function noteTitle(text: string, name: string) {
  return /^# (.+)$/m.exec(text)?.[1] ?? name.replace(/\.md$/, '').replace(/[-_]/g, ' ');
}
