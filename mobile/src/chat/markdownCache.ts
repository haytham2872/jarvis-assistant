import { lexer } from 'marked';

const parsed = new Map<string, ReturnType<typeof lexer> | null>();
/** Exact-text LRU for finished answers; transient drafts never occupy or renew an entry. */
export function markdownTokens(text: string, { keep = true }: { keep?: boolean } = {}) {
  if (keep && parsed.has(text)) {
    const value = parsed.get(text)!;
    parsed.delete(text);
    parsed.set(text, value);
    return value;
  }
  let value: ReturnType<typeof lexer> | null;
  try { value = lexer(text); } catch { value = null; }
  if (keep) {
    parsed.set(text, value);
    if (parsed.size > 200) parsed.delete(parsed.keys().next().value!);
  }
  return value;
}
