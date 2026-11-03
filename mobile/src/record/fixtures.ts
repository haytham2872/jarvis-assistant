// Invented, realistic wire shapes for tests; never imported by the app.
import type { RecordCall, RecordView } from '../chat/types';
export const call = (over: Partial<RecordCall> = {}): RecordCall => ({ id: 17,
  claim: 'The FOMC will leave the target range unchanged at its next meeting', criteria: 'The next statement retains the previous target range',
  domain: 'markets', raw_p: .731, stated_p: .67, outcome: 1, method: 'web', resolver: 'web',
  resolution_quote: 'The Committee decided to maintain the target range.', check_query: 'FOMC September 2026 official decision',
  resolve_by: Date.parse('2026-09-29T21:59Z') / 1000, created_at: Date.parse('2026-09-20T10:30Z') / 1000,
  resolved_at: Date.parse('2026-10-01T00:15Z') / 1000, source_quote: 'The probability I stated in the September review.', ...over });
export const empty = (over: Partial<RecordView> = {}): RecordView => ({ available: true, summary: { n: 0 }, summary_gut: { n: 0 },
  bands: [], bands_gut: [], by_month: [], by_domain: [], settled: [], open: [], void_count: 0, withdrawn_count: 0,
  early: true, threshold: 30, text: 'No forecasts logged yet.', caution: '', notes: [], ...over });
export const thin = (): RecordView => empty({ summary: { n: 1, brier: .1089, se: null, base_rate: 1, skill: null },
  summary_gut: { n: 1, brier: .072361, se: null, base_rate: 1, skill: null }, settled: [call()],
  open: [call({ id: 18, claim: 'The next stable CPython release will ship before November', domain: 'tech', raw_p: .917, stated_p: .84,
    outcome: null, resolved_at: null, resolver: '', resolution_quote: '', resolve_by: Date.parse('2026-10-31T22:59Z') / 1000,
    criteria: 'The final release appears on python.org before 1 November',
    check_query: 'Python stable final release October 2026' })],
  bands: [{ lo: .6, hi: .8, n: 1, hits: 1, mean_p: .67, interval: [.20654329147389294, 1] }],
  bands_gut: [{ lo: .6, hi: .8, n: 1, hits: 1, mean_p: .731, interval: [.20654329147389294, 1] }],
  by_month: [{ month: '2026-10', n: 1, brier: .1089, brier_gut: .072361 }],
  by_domain: [{ domain: 'markets', n: 1, brier: .1089, brier_gut: .072361 }],
  text: 'Track record: 1 resolved forecast(s), 1 open.\nBrier 0.109 (0 is perfect; always saying 50% scores 0.250).\nToo early to tell: under 30 resolved claims these numbers are mostly noise.',
  caution: 'Too early to tell: under 30 resolved claims these numbers are mostly noise.' });
