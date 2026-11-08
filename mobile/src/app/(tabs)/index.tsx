// The conversation (docs/archive/MOBILE-APP-PLAN.md M1-M4, slice 1 of the 15 October plan): what JARVIS and
// The owner said in the current phone chat, live answers as they stream, a composer that types or
// records, answers read aloud, and the orb. Slice 1 puts it in a tab, on the blue backdrop,
// with its controls in Liquid Glass floating over the messages.
import { LinearGradient } from 'expo-linear-gradient';
import { router, useFocusEffect, useLocalSearchParams } from 'expo-router';
import { SymbolView } from 'expo-symbols';
import { memo, useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react';
import {
  ActionSheetIOS, ActivityIndicator, Alert, FlatList, Linking, KeyboardAvoidingView, Platform, Pressable, StyleSheet, Text, View,
} from 'react-native';
import { SafeAreaView as TabSafeArea } from 'react-native-screens/experimental';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { FileChips } from '../../attach/AttachTray';
import { room, sendable, splitMarker, type FileChip } from '../../attach/model';
import { pick, PermissionRefused, useAttachments, type Source } from '../../attach/useAttachments';
import { ApprovalTray, JobCard, jobConclusion } from '../../chat/ActionCards';
import { Composer } from '../../chat/Composer';
import type { RequestStatus } from '../../chat/types';
import { ConversationMode } from '../../chat/ConversationMode';
import { Markdown } from '../../chat/Markdown';
import { ORB_COLOR } from '../../chat/Orb';
import { NeuralOrb } from '../../orb/NeuralOrb';
import { createTimeline, waitingApprovals, type LocalState, type TimelineItem } from '../../chat/reducer';
import { mark, since } from '../../core/perf';
import { Work } from '../../chat/Work';
import { useApp } from '../../state/AppContext';
import { backdrop, color, font, radius, space } from '../../theme/tokens';
import { Backdrop } from '../../ui/Backdrop';
import { Glass } from '../../ui/Glass';
import { ORB_LABEL, orbMode } from '../../voice/levels';
import type { SpeakState } from '../../voice/speechQueue';
import { IDLE } from '../../voice/speechQueue';

const STATE_LABEL: Record<LocalState, string> = {
  sending: 'Sending…', queued: 'Waiting for JARVIS', running: 'Thinking…', answered: '', absorbed: '',
  failed: 'JARVIS could not answer this', interrupted: 'Cut off before it finished', cancelled: 'Dropped with the old chat',
  unknown: 'Status unknown', send_failed: 'Not sent', transcribing: 'Transcribing…',
};

const HEADER_H = 56;
const HEADER_ORB = 40;              // slice 6: the neural orb as a badge beside the name

export default function Conversation() {
  const insets = useSafeAreaInsets();
  const { cfg, checked, prefs, levels, recording, setRecording, speaker, chat, reload } = useApp();
  const { state, send, sendRecording, adoptVoiceTurn, warmUp, retry, startNewChat, decide, stop, showCommand } = chat;
  const [draft, setDraft] = useState('');
  const params = useLocalSearchParams<{ record?: string; draft?: string; reply?: string; replyLabel?: string; replyText?: string }>();
  // Slice 4: "Ask about this" in the Inbox. The next message (typed or spoken) answers that one.
  const [replyTo, setReplyTo] = useState<{ id: number; label: string; text: string } | null>(null);
  const [autoRecord, setAutoRecord] = useState(false);
  const [talking, setTalking] = useState(false);       // slice 5: conversation mode is open

  useFocusEffect(useCallback(() => {
    void reload();                                  // back from Settings: maybe newly paired
    if (params.record === '1') {
      setAutoRecord(true);
      router.setParams({ record: undefined });
    }
    if (params.draft) {                             // "Ask JARVIS" from another tab: typed, not sent
      setDraft(params.draft);
      router.setParams({ draft: undefined });
    }
    const id = Number(params.reply);
    if (params.reply && Number.isInteger(id) && id > 0) {
      setReplyTo({ id, label: params.replyLabel ?? 'A message', text: params.replyText ?? '' });
      router.setParams({ reply: undefined, replyLabel: undefined, replyText: undefined });
    }
  }, [reload, params.record, params.draft, params.reply, params.replyLabel, params.replyText]));

  const selectTimeline = useMemo(() => createTimeline(), []);
  const items = useMemo(() => selectTimeline(state).reverse(),
    // Connection, mood and approvals do not change a conversation row.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [state.exchanges, state.requests, state.order, state.drafts, state.activity, state.steps, state.plans, state.jobs, selectTimeline]);
  const painted = useRef(false);
  const onPaint = useCallback(() => {
    if (painted.current) return;
    painted.current = true;
    mark('chat:first-paint');
    since('app:start', 'chat:first-paint');
  }, []);
  const waiting = useMemo(() => waitingApprovals(state), [state]);
  const mode = orbMode({ recording, speech: speaker.state, states: Object.values(state.requests).map((r) => r.state) });

  const attach = useAttachments(cfg);
  const onSend = () => {
    const text = draft.trim();
    const { ready, busy } = sendable(attach.items);
    if (busy || (!text && !ready.length)) return;
    setDraft('');
    void send(text, replyTo?.id, ready.length
      ? { uploads: ready.map((p) => p.uploadId!), files: ready.map((p) => ({ kind: p.kind, name: p.name })) }
      : undefined);
    attach.clear();
    setReplyTo(null);
  };

  /** Slice 7: the paperclip. Photos, the camera, or the Files app. */
  const choose = useCallback(async (source: Source) => {
    try {
      attach.add(await pick(source, room(attach.items)));
    } catch (e) {
      const what = e instanceof PermissionRefused ? 'The camera is off for Expo Go.' : 'That could not be opened.';
      Alert.alert(what, e instanceof PermissionRefused ? 'Turn it on in the iPhone Settings.' : undefined,
        e instanceof PermissionRefused
          ? [{ text: 'Not now', style: 'cancel' }, { text: 'Open Settings', onPress: () => void Linking.openSettings() }]
          : undefined);
    }
  }, [attach]);
  const onAttach = useCallback(() => {
    const options: { label: string; source: Source }[] = [
      { label: 'Photo library', source: 'library' }, { label: 'Take a photo', source: 'camera' },
      { label: 'File', source: 'files' },
    ];
    if (Platform.OS === 'ios') {
      ActionSheetIOS.showActionSheetWithOptions(
        { options: [...options.map((o) => o.label), 'Cancel'], cancelButtonIndex: options.length },
        (i) => { if (i < options.length) void choose(options[i].source); });
    } else {
      Alert.alert('Attach', undefined, [...options.map((o) => ({ text: o.label, onPress: () => void choose(o.source) })),
        { text: 'Cancel', style: 'cancel' }]);
    }
  }, [choose]);

  const onRecordStart = useCallback(() => {
    speaker.stop();            // the microphone and JARVIS's voice never overlap
    warmUp();
  }, [speaker, warmUp]);

  const onRecording = useCallback((uri: string, ms: number, requestId?: string) => {
    const answering = replyTo?.id;
    setReplyTo(null);
    return sendRecording(uri, ms, prefs.speakVoiceReplies, answering, requestId);
  }, [sendRecording, prefs.speakVoiceReplies, replyTo]);

  /** A live voice note the server already submitted: it joins the chat like an uploaded one. */
  const live = useMemo(() => ({
    spoken: prefs.speakVoiceReplies,
    replyTo: replyTo?.id,
    onTurn: (id: string, status: RequestStatus, audioUri?: string) => {
      setReplyTo(null);
      adoptVoiceTurn(id, status, prefs.speakVoiceReplies, replyTo?.id, audioUri);
    },
  }), [adoptVoiceTurn, prefs.speakVoiceReplies, replyTo]);

  /** A misheard word is fixed by a new message, never by rewriting what was sent. */
  const onCorrect = useCallback((heard: string) => setDraft(`I meant: ${heard}`), []);
  const speakRef = useRef(speaker.toggle);
  useLayoutEffect(() => { speakRef.current = speaker.toggle; }, [speaker.toggle]);
  const onSpeak = useCallback((source: string, text: string) => speakRef.current(source, text), []);
  const renderRow = useCallback(({ item }: { item: TimelineItem }) => {
    const source = item.kind === 'request' ? `req:${item.request.request_id}`
      : item.kind === 'exchange' ? item.request_id ? `req:${item.request_id}` : `ex:${item.id}` : null;
    return <Row item={item} onRetry={retry} onCorrect={onCorrect}
      speech={source && speaker.state.source === source ? speaker.state : IDLE} onSpeak={onSpeak} />;
  }, [retry, onCorrect, speaker.state, onSpeak]);

  const onNewChat = () => Alert.alert('New chat?', 'The current chat moves to your history.', [
    { text: 'Cancel', style: 'cancel' },
    { text: 'New chat', onPress: () => {
      speaker.stop();
      void startNewChat().then((m) => m !== 'New chat.' && Alert.alert(m));
    } },
  ]);

  const dot = state.connection === 'live' ? color.ok : state.connection === 'connecting' ? color.warn : color.err;
  const speaking = speaker.state.phase !== 'idle';

  return (
    <View style={styles.screen} onLayout={onPaint}>
      <Backdrop />
      <TabSafeArea edges={{ bottom: true }} style={styles.fill}>
        <KeyboardAvoidingView style={styles.fill} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
          {!checked ? <View style={styles.fill} /> : !cfg ? (
            <View style={[styles.empty, { paddingTop: insets.top + HEADER_H }]}>
              <Text style={styles.emptyTitle}>Not paired yet</Text>
              <Text style={styles.emptyText}>Scan the pairing QR in Settings to reach JARVIS.</Text>
              <Pressable accessibilityRole="button" onPress={() => router.push('/settings')}>
                <Glass tone="accent" interactive style={styles.cta}><Text style={styles.ctaText}>Open Settings</Text></Glass>
              </Pressable>
            </View>
          ) : (
            <FlatList
              style={styles.fill}
              contentContainerStyle={styles.listContent}
              data={items}
              inverted
              keyExtractor={(i) => i.key}
              renderItem={renderRow}
              initialNumToRender={10}
              windowSize={7}
              maxToRenderPerBatch={6}
              extraData={speaker.state}
              keyboardDismissMode="interactive"
              // inverted: the footer is the top, where the floating header sits over the oldest message
              ListFooterComponent={<View style={{ height: insets.top + HEADER_H }} />}
              ListEmptyComponent={state.loaded ? (
                <Text style={[styles.emptyText, styles.flip]}>Say something to JARVIS.</Text>
              ) : <ActivityIndicator color={color.tMid} style={styles.flip} />}
            />
          )}
          <ApprovalTray approvals={waiting} onDecide={decide} />
          {replyTo ? <ReplyChip label={replyTo.label} text={replyTo.text} onClear={() => setReplyTo(null)} /> : null}
          <Composer cfg={cfg} onCommandReply={showCommand} enabled={!!cfg} draft={draft} setDraft={setDraft} onSendText={onSend}
            onRecordStart={onRecordStart} onRecording={onRecording} live={live} levels={levels} onActiveChange={setRecording}
            autoStart={autoRecord} onAutoStarted={() => setAutoRecord(false)}
            onConversation={() => { speaker.stop(); setTalking(true); }}
            attachments={attach.items} onAttach={onAttach} onRemoveAttachment={attach.remove}
            onRetryAttachment={attach.retry}
            onStop={Object.values(state.requests).some((r) => r.state === 'running') ? () => void stop() : undefined} />
        </KeyboardAvoidingView>
      </TabSafeArea>

      {talking ? <ConversationMode onClose={() => setTalking(false)} /> : null}

      {/* The header floats: messages scroll under its glass controls, faded at the very top. */}
      <View style={[styles.header, { paddingTop: insets.top }]} pointerEvents="box-none">
        <LinearGradient colors={[backdrop.colors[0], 'rgba(13,33,54,0)']} style={StyleSheet.absoluteFill} pointerEvents="none" />
        <Pressable style={styles.identity} accessibilityRole={speaking ? 'button' : undefined}
          accessibilityLabel={speaking ? 'Stop reading' : undefined} onPress={speaking ? speaker.stop : undefined}>
          <NeuralOrb mode={mode} bus={levels} size={HEADER_ORB} mood={state.mood} haptics={!talking} />
          <View>
            <View style={styles.titleRow}>
              <Text style={styles.title}>JARVIS</Text>
              <View style={[styles.dot, { backgroundColor: cfg ? dot : color.tDim }]}
                accessibilityLabel={`Connection ${cfg ? state.connection : 'not paired'}`} />
            </View>
            <Text style={[styles.orbLabel, { color: ORB_LABEL[mode] ? ORB_COLOR[mode] : color.tDim }]}>
              {ORB_LABEL[mode] ? (speaking ? `${ORB_LABEL[mode]} · tap to stop` : ORB_LABEL[mode]) : 'Ready'}
            </Text>
          </View>
        </Pressable>
        <Glass interactive style={styles.actions}>
          <HeaderAction symbol="clock.arrow.circlepath" label="Chats" onPress={() => router.push('/history')} disabled={!cfg} />
          <HeaderAction symbol="square.and.pencil" label="New chat" onPress={onNewChat} disabled={!cfg} />
          <HeaderAction symbol="gearshape" label="Settings" onPress={() => router.push('/settings')} />
        </Glass>
      </View>
    </View>
  );
}

/** Slice 4: the Inbox message the next message answers, with a way to drop it. */
function ReplyChip({ label, text, onClear }: { label: string; text: string; onClear: () => void }) {
  return (
    <Glass tone="blue" style={styles.reply} testID="reply-chip">
      <View style={styles.replyBar} />
      <View style={styles.replyBody}>
        <Text style={styles.replyLabel} numberOfLines={1}>Answering: {label}</Text>
        {text ? <Text style={styles.replyText} numberOfLines={1}>{text}</Text> : null}
      </View>
      <Pressable accessibilityRole="button" accessibilityLabel="Stop answering this message" onPress={onClear}
        hitSlop={8} style={({ pressed }) => [styles.replyClear, pressed && styles.pressed]}>
        <SymbolView name="xmark.circle.fill" size={18} tintColor={color.tMid}
          fallback={<Text style={styles.actionFallback}>×</Text>} />
      </Pressable>
    </Glass>
  );
}

function HeaderAction(p: { symbol: 'clock.arrow.circlepath' | 'square.and.pencil' | 'gearshape'; label: string; onPress: () => void; disabled?: boolean }) {
  return (
    <Pressable accessibilityRole="button" accessibilityLabel={p.label} onPress={p.onPress} disabled={p.disabled}
      hitSlop={4} style={({ pressed }) => [styles.action, p.disabled && styles.off, pressed && styles.pressed]}>
      <SymbolView name={p.symbol} size={19} weight="medium" tintColor={color.tHi}
        fallback={<Text style={styles.actionFallback}>{p.label[0]}</Text>} />
    </Pressable>
  );
}

type RowProps = {
  item: TimelineItem;
  onRetry: (id: string) => void;
  onCorrect: (heard: string) => void;
  speech: SpeakState;
  onSpeak: (source: string, text: string) => void;
};

const Row = memo(function Row({ item, onRetry, onCorrect, speech, onSpeak }: RowProps) {
  if (item.kind === 'job') {
    const conclusion = jobConclusion(item.job);
    return (
      <View style={styles.pair}>
        <JobCard job={item.job} />
        {conclusion ? <Markdown text={conclusion} /> : null}
      </View>
    );
  }
  if (item.kind === 'exchange') {
    return (
      <View style={styles.pair}>
        <UserBubble text={item.user} voice={item.voice} onCorrect={onCorrect} />
        <Work plan={item.plan} steps={item.steps} running={false} />
        <Markdown text={item.assistant} />
        {/* The same answer keeps one source as it moves from request to history. */}
        <SpeakButton source={item.request_id ? `req:${item.request_id}` : `ex:${item.id}`} text={item.assistant}
          speech={speech} onSpeak={onSpeak} />
      </View>
    );
  }
  const r = item.request;
  const label = STATE_LABEL[r.state];
  const reply = r.answer ?? item.draft ?? '';
  const retryable = r.state === 'send_failed';
  return (
    <View style={styles.pair}>
      <UserBubble text={r.text} voice={r.input === 'voice'} onCorrect={onCorrect} files={r.files} />
      {r.problems?.length ? (
        <Text style={styles.problems}>Couldn't read {r.problems.join('; ')}.</Text>
      ) : null}
      <Work plan={item.plan} steps={item.steps} thinking={item.activity} running={r.state === 'running'} />
      {reply ? <Markdown text={reply} streaming={!r.answer} /> : null}
      {r.state === 'answered' && r.answer ? (
        <SpeakButton source={`req:${r.request_id}`} text={r.answer} speech={speech} onSpeak={onSpeak} />
      ) : null}
      {label ? (
        <View style={styles.statusRow}>
          {r.state === 'running' || r.state === 'sending' || r.state === 'queued' || r.state === 'transcribing'
            ? <ActivityIndicator size="small" color={color.tMid} /> : null}
          <Text style={[styles.status, (r.state === 'failed' || r.state === 'send_failed') && { color: color.err }]}>
            {label}{r.error ? `: ${r.error}` : ''}
          </Text>
          {retryable ? (
            <Pressable accessibilityRole="button" onPress={() => onRetry(r.request_id)} style={styles.retry}>
              <Text style={styles.retryText}>Retry</Text>
            </Pressable>
          ) : null}
        </View>
      ) : null}
    </View>
  );
});

/** Read this answer aloud, or stop it. Only final answers have one. */
function SpeakButton(p: { source: string; text: string; speech: SpeakState; onSpeak: (s: string, t: string) => void }) {
  const mine = p.speech.source === p.source && p.speech.phase !== 'idle';
  const label = !mine ? 'Read aloud' : p.speech.phase === 'loading' ? 'Preparing' : 'Stop reading';
  return (
    <Pressable accessibilityRole="button" accessibilityLabel={label} onPress={() => p.onSpeak(p.source, p.text)}
      hitSlop={10} style={({ pressed }) => [styles.speak, pressed && styles.pressed]}>
      {mine && p.speech.phase === 'loading'
        ? <ActivityIndicator size="small" color={color.tMid} />
        : <SymbolView name={mine ? 'stop.fill' : 'speaker.wave.2'} size={15}
            tintColor={mine ? color.stSpeaking : color.tMid}
            fallback={<Text style={styles.voiceMark}>{mine ? 'Stop' : 'Play'}</Text>} />}
      {mine && p.speech.voice === 'phone' ? <Text style={styles.speakNote}>iPhone voice</Text> : null}
    </Pressable>
  );
}

/** His words. A voice message shows what was heard; tapping it starts a correction. Files he
 * sent show as chips: from the server's line naming them, or before that from what he picked. */
function UserBubble({ text: raw, voice, onCorrect, files: picked }: {
  text: string; voice?: boolean; onCorrect: (heard: string) => void; files?: FileChip[];
}) {
  const { text, files: named } = splitMarker(raw);
  const files = named.length ? named : picked ?? [];
  if (!voice) {
    return (
      <View style={styles.userWrap}>
        {text ? <Text style={styles.user} selectable>{text}</Text> : null}
        <FileChips files={files} />
      </View>
    );
  }
  return (
    <Pressable accessibilityRole="button" accessibilityLabel={text ? `You said: ${text}` : 'Voice message'}
      accessibilityHint={text ? 'Correct what was heard' : undefined} disabled={!text}
      onPress={() => onCorrect(text)} style={({ pressed }) => [styles.user, styles.voice, pressed && styles.pressed]}>
      <SymbolView name="waveform" size={13} tintColor={color.stIdle}
        fallback={<Text style={styles.voiceMark}>~</Text>} />
      <Text style={[styles.userText, !text && styles.placeholder]}>{text || 'Voice message'}</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.bg },
  fill: { flex: 1 },
  flip: { transform: [{ scaleY: -1 }] },
  listContent: { paddingHorizontal: space.s4, paddingTop: space.s3, gap: space.s5 },
  header: {
    position: 'absolute', left: 0, right: 0, top: 0, flexDirection: 'row', alignItems: 'center',
    justifyContent: 'space-between', paddingHorizontal: space.s4, paddingBottom: space.s2, minHeight: HEADER_H,
  },
  identity: { flexDirection: 'row', alignItems: 'center', gap: space.s2 },
  titleRow: { flexDirection: 'row', alignItems: 'center', gap: space.s2 },
  dot: { width: 7, height: 7, borderRadius: 4 },
  title: { color: color.tHi, fontSize: font.read, fontWeight: '600', letterSpacing: 3 },
  orbLabel: { fontSize: font.micro, fontWeight: '600', letterSpacing: 0.4, marginTop: 1 },
  actions: { flexDirection: 'row', alignItems: 'center', borderRadius: 22, paddingHorizontal: space.s1, height: 44 },
  action: { width: 40, height: 44, alignItems: 'center', justifyContent: 'center' },
  actionFallback: { color: color.tHi, fontSize: font.body, fontWeight: '600' },
  pair: { gap: space.s2 },
  user: {
    alignSelf: 'flex-end', maxWidth: '85%', color: color.tHi, fontSize: font.body, lineHeight: 21, overflow: 'hidden',
    backgroundColor: backdrop.bubble, borderColor: backdrop.bubbleEdge, borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 18, borderBottomRightRadius: 6, paddingHorizontal: space.s3 + 2, paddingVertical: space.s2,
  },
  userWrap: { alignSelf: 'flex-end', maxWidth: '86%', alignItems: 'flex-end' },
  problems: { color: color.err, fontSize: font.label, alignSelf: 'flex-end' },
  userText: { flexShrink: 1, color: color.tHi, fontSize: font.body, lineHeight: 21 },
  voice: { flexDirection: 'row', alignItems: 'center', gap: space.s2 },
  voiceMark: { color: color.tMid, fontSize: font.label },
  placeholder: { color: color.tMid, fontStyle: 'italic' },
  speak: { flexDirection: 'row', alignItems: 'center', gap: space.s2, alignSelf: 'flex-start', minHeight: 28 },
  speakNote: { color: color.tDim, fontSize: font.micro },
  statusRow: { flexDirection: 'row', alignItems: 'center', gap: space.s2, alignSelf: 'flex-end' },
  status: { color: color.tMid, fontSize: font.label },
  retry: { paddingHorizontal: space.s2, paddingVertical: space.s1, borderRadius: radius.sm, backgroundColor: color.surface2 },
  retryText: { color: color.stIdle, fontSize: font.label },
  empty: { flex: 1, alignItems: 'center', justifyContent: 'center', gap: space.s3, padding: space.s5 },
  emptyTitle: { color: color.tHi, fontSize: font.read },
  emptyText: { color: color.tMid, fontSize: font.body, textAlign: 'center' },
  cta: { borderRadius: 22, paddingHorizontal: space.s5, height: 44, alignItems: 'center', justifyContent: 'center' },
  ctaText: { color: color.bg, fontSize: font.body, fontWeight: '600' },
  reply: {
    flexDirection: 'row', alignItems: 'center', gap: space.s2, marginHorizontal: space.s4, marginBottom: space.s2,
    borderRadius: 14, paddingVertical: space.s2, paddingRight: space.s2, overflow: 'hidden',
  },
  replyBar: { width: 3, alignSelf: 'stretch', backgroundColor: color.stIdle, borderRadius: 2, marginLeft: space.s2 },
  replyBody: { flex: 1 },
  replyLabel: { color: color.stIdle, fontSize: font.label, fontWeight: '600' },
  replyText: { color: color.tMid, fontSize: font.label },
  replyClear: { padding: space.s1 },
  off: { opacity: 0.4 },
  pressed: { opacity: 0.7 },
});
