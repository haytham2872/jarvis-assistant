import { File } from 'expo-file-system';
import { useFocusEffect } from 'expo-router';
import { useCallback, useLayoutEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react';
import { AppState, Text, TextInput, View } from 'react-native';
import { transcribeGoalAnswer } from '../chat/client';
import type { ServerConfig } from '../core/api';
import { useApp } from '../state/AppContext';
import { color } from '../theme/tokens';
import { Glass } from '../ui/Glass';
import { useRecorder } from '../voice/useRecorder';
import { Button, explainGoal } from './Screen';
import { styles as s } from './styles';

/** Voice fills the same editable field as typing. It never submits an answer on its own. */
export function Question({ question, cfg, disabled, recording, onRecording, onAnswer }:
  { question: { wait_id: string; text: string }; cfg: ServerConfig; disabled: boolean; recording: string | null;
    onRecording: Dispatch<SetStateAction<string | null>>; onAnswer: (wait: string, text: string) => Promise<boolean> }) {
  const { speaker, setRecording } = useApp();
  const [text, setText] = useState('');
  const [error, setError] = useState('');
  const [phase, setPhase] = useState<'idle' | 'starting' | 'recording' | 'stopping' | 'transcribing'>('idle');
  const alive = useRef(true);
  const owned = useRef(false);
  const microphone = useRef(false);
  const started = useRef(false);
  const finishing = useRef(false);
  const cancelling = useRef(false);
  const generation = useRef(0);
  const release = useCallback(() => {
    if (microphone.current) setRecording(false);
    microphone.current = false;
    owned.current = false;
    onRecording((current) => current === question.wait_id ? null : current);
  }, [onRecording, question.wait_id, setRecording]);
  const cap = useRef<() => void>(() => {});
  const rec = useRecorder(useCallback(() => cap.current(), []));
  const recRef = useRef(rec);
  useLayoutEffect(() => { recRef.current = rec; }, [rec]);
  useFocusEffect(useCallback(() => {
    alive.current = true;
    const stop = () => {
      alive.current = false;
      generation.current++;
      if (started.current && !finishing.current) {
        started.current = false; cancelling.current = true; setPhase('stopping');
        void recRef.current.cancel().catch(() => undefined).finally(() => {
          cancelling.current = false; release(); setPhase('idle');
        });
      } else if (!owned.current) setPhase('idle');
      // A pending start/finish retains the microphone until its own cleanup completes.
    };
    const sub = AppState.addEventListener('change', (state) => { if (state !== 'active') stop(); else alive.current = true; });
    return () => { sub.remove(); stop(); };
  }, [release]));

  const finish = useCallback(async () => {
    if (finishing.current || !started.current) return;
    finishing.current = true;
    const version = generation.current;
    started.current = false;
    setPhase('transcribing');
    let uri: string | undefined;
    try {
      const result = await recRef.current.finish();
      uri = result?.uri;
      if (microphone.current) setRecording(false);
      microphone.current = false;
      if (!alive.current || version !== generation.current) return;
      if (!result || result.ms < 500) { setError('That recording was too short. Try again.'); return; }
      const heard = await transcribeGoalAnswer(cfg, question.wait_id, result.uri);
      if (alive.current && version === generation.current) { setText(heard); setError(''); }
    } catch (e) { if (alive.current && version === generation.current) setError(explainGoal(e)); }
    finally {
      if (uri) { try { new File(uri).delete(); } catch { /* already removed */ } }
      setPhase('idle');
      release(); finishing.current = false;
    }
  }, [cfg, question.wait_id, release, setRecording]);
  useLayoutEffect(() => { cap.current = () => void finish(); }, [finish]);
  const start = async () => {
    if (owned.current || finishing.current || cancelling.current || phase !== 'idle' || disabled || recording) return;
    const version = generation.current;
    owned.current = true;
    setPhase('starting'); setError(''); onRecording(question.wait_id); speaker.stop();
    try {
      const failure = await rec.start();
      started.current = !failure;
      if (!alive.current || version !== generation.current) {
        if (!failure) await rec.cancel(); started.current = false; release(); setPhase('idle'); return;
      }
      if (failure) {
        setError(failure === 'permission' ? 'Allow the microphone for Expo Go in iPhone Settings.' : 'The microphone could not start.');
        setPhase('idle'); release();
      } else { setPhase('recording'); microphone.current = true; setRecording(true); }
    } catch (e) { setError(explainGoal(e)); setPhase('idle'); release(); }
  };
  const locked = disabled || (recording !== null && recording !== question.wait_id) || !['idle', 'recording'].includes(phase);
  return <Glass tone="blue" style={s.card}>
    <Text style={s.section}>Waiting for you</Text><Text style={s.heading}>{question.text}</Text>
    <TextInput accessibilityLabel={`Answer: ${question.text}`} placeholder="Your answer" placeholderTextColor={color.tDim}
      multiline maxLength={1500} style={s.input} value={text} onChangeText={setText} editable={!locked && phase === 'idle'} />
    <Text style={s.dim}>{phase === 'transcribing' ? 'Turning your recording into text…' : phase === 'stopping' ? 'Stopping microphone…' : 'Review your words, then tap Send answer.'}</Text>
    {error ? <Text style={s.error}>{error}</Text> : null}
    <View style={s.buttons}>
      <Button label={phase === 'recording' ? 'Finish recording' : 'Record answer'} disabled={locked}
        onPress={() => phase === 'recording' ? void finish() : void start()} />
      <Button label="Send answer" accent disabled={locked || phase !== 'idle' || !text.trim()}
        onPress={() => void onAnswer(question.wait_id, text.trim()).then((sent) => { if (sent && alive.current) setText(''); })} />
    </View>
  </Glass>;
}
