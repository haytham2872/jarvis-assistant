// Shared model-id -> display-label logic for the UI surfaces that badge a
// turn's model: TopStrip's status label (modelLabel, uppercase) and
// ConvoColumn/TranscriptOverlay's per-turn badge (modelBadge, lowercase).
// Previously three near-identical copies of this logic lived inline in each
// component; kept in one place so a new provider's id shape only needs
// teaching once. Existing Claude (haiku/sonnet/opus) and Kimi behavior is
// unchanged -- this only adds a rule for OpenRouter's vendor-prefixed
// DeepSeek ids, e.g. "deepseek/deepseek-v4-flash-0731" -> "v4 flash".

function deepseekSegment(model) {
  const seg = model.split('/').pop()
  return seg.replace(/^deepseek-/, '').replace(/-\d{4}$/, '').replace(/-/g, ' ')
}

export function modelLabel(model) {
  if (!model) return null
  const m = String(model)
  if (m.includes('sonnet')) return 'SONNET'
  if (m.includes('haiku'))  return 'HAIKU'
  if (m.includes('opus'))   return 'OPUS'
  if (m.includes('kimi'))   return m.toUpperCase().replace(/-\d+/g, '')
  if (m.includes('deepseek')) return deepseekSegment(m).toUpperCase()
  return m.replace(/-\d{8,}$/, '').toUpperCase()
}

export function modelBadge(model) {
  if (!model) return null
  const m = String(model)
  if (m.includes('sonnet')) return 'sonnet'
  if (m.includes('haiku'))  return 'haiku'
  if (m.includes('opus'))   return 'opus'
  if (m.includes('deepseek')) return deepseekSegment(m)
  return m.replace(/-\d{8,}$/, '')
}
