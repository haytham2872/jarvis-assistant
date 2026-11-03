// JARVIS's answers as formatted text (docs/archive/MOBILE-APP-PLAN.md M4). marked's tokenizer (pure JS,
// so it runs in Expo Go) turns the Markdown into tokens; this file draws them with the
// app's own tokens. Links open in Safari, and only web and mail links do. A half-streamed
// draft renders too: an unfinished code fence simply reads as code until it closes.
import { type Token, type Tokens } from 'marked';
import { createContext, memo, useContext, useMemo, type ReactNode } from 'react';
import { Linking, ScrollView, StyleSheet, Text, View } from 'react-native';

import { color, font, mono, radius, space } from '../theme/tokens';
import { markdownTokens } from './markdownCache';

const SAFE_LINK = /^(https?:|mailto:)/i;
const EvidenceLink = createContext<((id: string) => void) | undefined>(undefined);

function MarkdownLink({ href, children }: { href: string; children: ReactNode }) {
  const onEvidence = useContext(EvidenceLink);
  const id = /^jarvis-evidence:(ev[A-Za-z0-9_-]+)$/.exec(href)?.[1];
  return <Text style={styles.link} accessibilityRole="link"
    onPress={() => id && onEvidence ? onEvidence(id) : openLink(href)}>{children}</Text>;
}

export function openLink(href: string): boolean {
  if (!SAFE_LINK.test(href)) return false;
  void Linking.openURL(href).catch(() => undefined);
  return true;
}

function inline(tokens: Token[] | undefined, key: string): ReactNode[] {
  return (tokens ?? []).map((t, i) => {
    const k = `${key}.${i}`;
    switch (t.type) {
      case 'strong':
        return <Text key={k} style={styles.strong}>{inline((t as Tokens.Strong).tokens, k)}</Text>;
      case 'em':
        return <Text key={k} style={styles.em}>{inline((t as Tokens.Em).tokens, k)}</Text>;
      case 'del':
        return <Text key={k} style={styles.del}>{inline((t as Tokens.Del).tokens, k)}</Text>;
      case 'codespan':
        return <Text key={k} style={styles.codespan}>{(t as Tokens.Codespan).text}</Text>;
      case 'br':
        return '\n';
      case 'link': {
        const link = t as Tokens.Link;
        return (
          <MarkdownLink key={k} href={link.href}>
            {inline(link.tokens, k)}
          </MarkdownLink>
        );
      }
      case 'image':
        return <Text key={k} style={styles.link} onPress={() => openLink((t as Tokens.Image).href)}>{(t as Tokens.Image).text || 'image'}</Text>;
      case 'text':
        return 'tokens' in t && t.tokens ? <Text key={k}>{inline(t.tokens, k)}</Text> : decode((t as Tokens.Text).text);
      case 'escape':
        return (t as Tokens.Escape).text;
      default:
        return 'text' in t ? decode(String(t.text)) : '';
    }
  });
}

const ENTITIES: Record<string, string> = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'" };
function decode(s: string): string {
  return s.replace(/&(amp|lt|gt|quot|#39);/g, (m) => ENTITIES[m] ?? m);
}

function blocks(tokens: Token[], key: string): ReactNode[] {
  return tokens.map((t, i) => {
    const k = `${key}/${i}`;
    switch (t.type) {
      case 'space':
      case 'def':
        return null;
      case 'paragraph':
        return <Text key={k} style={styles.p} selectable>{inline((t as Tokens.Paragraph).tokens, k)}</Text>;
      case 'text':                         // a list item's text, without paragraph spacing
        return (
          <Text key={k} style={styles.p} selectable>
            {'tokens' in t && t.tokens ? inline(t.tokens, k) : decode((t as Tokens.Text).text)}
          </Text>
        );
      case 'heading': {
        const h = t as Tokens.Heading;
        return (
          <Text key={k} style={[styles.p, styles.heading, h.depth <= 1 ? styles.h1 : h.depth === 2 ? styles.h2 : null]}
            accessibilityRole="header" selectable>
            {inline(h.tokens, k)}
          </Text>
        );
      }
      case 'code':
        return (
          <ScrollView key={k} horizontal style={styles.codeBlock} contentContainerStyle={styles.codeInner}>
            <Text style={styles.codeText} selectable>{(t as Tokens.Code).text}</Text>
          </ScrollView>
        );
      case 'blockquote':
        return <View key={k} style={styles.quote}>{blocks((t as Tokens.Blockquote).tokens, k)}</View>;
      case 'hr':
        return <View key={k} style={styles.hr} />;
      case 'list': {
        const list = t as Tokens.List;
        const start = typeof list.start === 'number' ? list.start : 1;
        return (
          <View key={k} style={styles.list}>
            {list.items.map((item, n) => (
              <View key={`${k}-${n}`} style={styles.item}>
                <Text style={styles.marker}>
                  {item.task ? (item.checked ? '☑' : '☐') : list.ordered ? `${start + n}.` : '•'}
                </Text>
                <View style={styles.itemBody}>{blocks(item.tokens, `${k}-${n}`)}</View>
              </View>
            ))}
          </View>
        );
      }
      case 'table': {
        const table = t as Tokens.Table;
        return (
          <ScrollView key={k} horizontal style={styles.table}>
            <View>
              <View style={[styles.row, styles.headRow]}>
                {table.header.map((cell, c) => (
                  <Text key={c} style={[styles.cell, styles.headCell]} selectable>{inline(cell.tokens, `${k}h${c}`)}</Text>
                ))}
              </View>
              {table.rows.map((row, r) => (
                <View key={r} style={styles.row}>
                  {row.map((cell, c) => (
                    <Text key={c} style={styles.cell} selectable>{inline(cell.tokens, `${k}r${r}c${c}`)}</Text>
                  ))}
                </View>
              ))}
            </View>
          </ScrollView>
        );
      }
      default:
        return 'text' in t && String(t.text).trim()
          ? <Text key={k} style={styles.p} selectable>{decode(String(t.text))}</Text> : null;
    }
  }).filter((n) => n !== null);
}

/** One virtualized note block, using the same renderer as chat. */
export const MarkdownBlock = memo(function MarkdownBlock({ token }: { token: Token }) {
  return <View style={styles.root}>{blocks([token], 'note')}</View>;
});

/** One answer, formatted. */
export const Markdown = memo(function Markdown({ text, onEvidence, streaming = false }:
  { text: string; onEvidence?: (id: string) => void; streaming?: boolean }) {
  const tokens = useMemo(() => markdownTokens(text ?? '', { keep: !streaming }), [text, streaming]);
  if (!tokens) return <Text style={styles.p} selectable>{text}</Text>;
  return <EvidenceLink value={onEvidence}><View style={styles.root}>{blocks(tokens, 'm')}</View></EvidenceLink>;
});

const styles = StyleSheet.create({
  root: { alignSelf: 'stretch', gap: space.s2 },
  p: { color: color.tHi, fontSize: font.body, lineHeight: 22 },
  strong: { fontWeight: '700' },
  em: { fontStyle: 'italic' },
  del: { textDecorationLine: 'line-through' },
  link: { color: color.stIdle, textDecorationLine: 'underline' },
  codespan: { fontFamily: mono, fontSize: font.label, backgroundColor: color.surface2, color: color.tHi },
  heading: { fontWeight: '700', marginTop: space.s1 },
  h1: { fontSize: font.read, lineHeight: 25 },
  h2: { fontSize: font.body + 1, lineHeight: 23 },
  codeBlock: {
    backgroundColor: color.surface, borderRadius: radius.md, borderColor: color.border,
    borderWidth: StyleSheet.hairlineWidth,
  },
  codeInner: { padding: space.s3 },
  codeText: { fontFamily: mono, fontSize: font.label, lineHeight: 19, color: color.tHi },
  quote: { borderLeftWidth: 3, borderLeftColor: color.borderBright, paddingLeft: space.s3, gap: space.s2 },
  hr: { height: StyleSheet.hairlineWidth, backgroundColor: color.borderBright, marginVertical: space.s1 },
  list: { gap: space.s1 },
  item: { flexDirection: 'row', gap: space.s2 },
  marker: { color: color.tMid, fontSize: font.body, lineHeight: 22, minWidth: 18, textAlign: 'right' },
  itemBody: { flex: 1, gap: space.s1 },
  table: { borderColor: color.border, borderWidth: StyleSheet.hairlineWidth, borderRadius: radius.md },
  row: { flexDirection: 'row', borderBottomColor: color.border, borderBottomWidth: StyleSheet.hairlineWidth },
  headRow: { backgroundColor: color.surface2 },
  cell: {
    minWidth: 90, maxWidth: 220, paddingHorizontal: space.s2, paddingVertical: space.s1 + 2,
    color: color.tHi, fontSize: font.label, lineHeight: 18,
  },
  headCell: { fontWeight: '700' },
});
