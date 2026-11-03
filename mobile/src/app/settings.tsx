// Settings: pairing with a server (M1), where JARVIS's own messages go and the phone's notifications
// (slice 4), how answers are read aloud (M3), the Action Button Shortcut (slice 1), and which build
// and display the phone is running.
import * as Clipboard from 'expo-clipboard';
import Constants from 'expo-constants';
import * as Haptics from 'expo-haptics';
import { router, useFocusEffect } from 'expo-router';
import { useCallback, useState } from 'react';
import { Pressable, ScrollView, StyleSheet, Switch, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { endpoint } from '../core/api';
import { parsePairing, tokenHint } from '../core/pairing';
import { Scanner } from '../core/Scanner';
import { describeUpdate } from '../core/version';
import { EMPTY, loadSettings, saveSettings, type Settings } from '../core/settings';
import { supported as notificationsHere } from '../notify/notify';
import { pushLine, ROUTES } from '../notify/plan';
import { useApp } from '../state/AppContext';
import { color, font, radius, space } from '../theme/tokens';
import { shareAddress } from '../attach/share';
import { Backdrop } from '../ui/Backdrop';
import { Glass, LIQUID_GLASS } from '../ui/Glass';
import { IconButton } from '../ui/IconButton';
import { DEFAULT_PREFS, loadPrefs, RATES, savePrefs, type VoicePrefs } from '../voice/prefs';

/** The Shortcut's steps, in order (Settings shows them under ACTION BUTTON). */
export const SHORTCUT_STEPS = [
  'In Shortcuts, tap + and name it "Ask JARVIS".',
  'Add "Dictate Text" (Stop Listening: After Pause). For French and English mixed, use "Record Audio" instead.',
  'Add "Get Contents of URL": paste the address, Method POST, a header "Authorization" with the pasted value, '
    + 'and Request Body JSON with a field "text" set to Dictated Text (or Request Body File set to Recorded Audio).',
  'Add "Get Dictionary Value" for the key "say", then "Speak Text" with it.',
  'In the iPhone Settings, Action Button, choose Shortcut, then Ask JARVIS.',
];

/** Slice 7: the share sheet's Shortcut (Settings shows it under SHARE SHEET). A photo, PDF,
 * document, link or text from any app goes to JARVIS, and his answer comes back. */
export const SHARE_STEPS = [
  'In Shortcuts, tap + and name it "Share to JARVIS". Tap the (i) and turn on Show in Share Sheet.',
  'Add "Ask for Input" (Text, prompt "Anything to ask about it?"; leaving it empty is fine), then "URL Encode" on Provided Input.',
  'Add "Get Contents of URL": paste the share address and put the URL Encoded Text right after its "q=", Method POST, '
    + 'a header "Authorization" with the pasted value, and Request Body File set to Shortcut Input.',
  'Add "Get Dictionary Value" for the key "say", then "Show Result" (or "Speak Text") with it.',
  'Now Share in Photos, Files, Safari or Notes lists Share to JARVIS. The exchange lands in this chat too.',
];

export default function SettingsScreen() {
  const insets = useSafeAreaInsets();
  const { reload, notices, cfg } = useApp();
  const [routeNote, setRouteNote] = useState('');
  const [settings, setSettings] = useState<Settings>(EMPTY);
  const [scanning, setScanning] = useState(false);
  const [note, setNote] = useState('');
  const [copied, setCopied] = useState('');
  const [prefs, setPrefs] = useState<VoicePrefs>(DEFAULT_PREFS);

  useFocusEffect(useCallback(() => {
    void loadSettings().then(setSettings);
    void loadPrefs().then(setPrefs);
  }, []));

  const changePrefs = (next: VoicePrefs) => {
    setPrefs(next);
    void savePrefs(next).then(reload);
  };

  const onScan = async (data: string) => {
    setScanning(false);
    const r = parsePairing(data);
    if (!r.ok) {
      setNote(`That code did not work: ${r.why}.`);
      return;
    }
    await saveSettings(r.settings);
    setSettings(r.settings);
    await reload();
    void Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
    setNote('Paired.');
  };

  const copy = async (what: 'address' | 'share' | 'token') => {
    const text = what === 'address' ? endpoint(settings.baseUrl, '/ask')
      : what === 'share' ? shareAddress(settings.baseUrl) : `Bearer ${settings.token}`;
    await Clipboard.setStringAsync(text);
    void Haptics.selectionAsync();
    setCopied(what === 'token' ? 'Authorization value copied. Paste it into the Shortcut only.'
      : what === 'share' ? 'Share address copied.' : 'Address copied.');
  };

  return (
    <View style={styles.screen}>
      <Backdrop />
      <ScrollView contentContainerStyle={{ paddingTop: insets.top + space.s2, paddingBottom: insets.bottom + space.s6,
        paddingHorizontal: space.s4, gap: space.s4 }}>
        <View style={styles.titleRow}>
          <IconButton symbol="chevron.left" label="Back" onPress={() => (router.canGoBack() ? router.back() : router.replace('/'))} />
          <Text style={styles.title}>Settings</Text>
        </View>

        <Card label="SERVER">
          <Text style={[styles.value, { color: settings.baseUrl ? color.ok : color.warn }]}>
            {settings.baseUrl ? `Paired with ${settings.baseUrl}` : 'Not paired yet'}
          </Text>
          {settings.baseUrl ? <Text style={styles.hint}>Token {tokenHint(settings.token)}</Text> : null}
          <Btn label="Scan pairing QR" onPress={() => setScanning(true)} />
          {note ? <Text style={styles.hint}>{note}</Text> : null}
          <Text style={styles.hint}>
            The QR comes from the PC: mobile/tools/pair_qr.mjs. Away from home, the Tailscale app must say Connected.
          </Text>
        </Card>

        {cfg ? (
          <Card label="JARVIS'S OWN MESSAGES">
            <Text style={styles.body}>
              The brief, reminders, scheduled checks, alerts, research findings and goal results. The Inbox keeps
              them all whichever you choose.
            </Text>
            <View style={styles.row}>
              {ROUTES.map((r) => (
                <Pressable key={r.value} accessibilityRole="button" accessibilityState={{ selected: notices.route === r.value }}
                  accessibilityLabel={`Send them to ${r.label}`}
                  onPress={() => void notices.setRoute(r.value).then(setRouteNote)}
                  style={({ pressed }) => pressed && { opacity: 0.7 }}>
                  <Glass tone={notices.route === r.value ? 'accent' : 'clear'} interactive style={styles.chip}>
                    <Text style={[styles.chipText, notices.route === r.value && styles.chipTextOn]}>{r.label}</Text>
                  </Glass>
                </Pressable>
              ))}
            </View>
            <Text style={styles.hint}>{ROUTES.find((r) => r.value === notices.route)?.detail}</Text>
            {routeNote ? <Text style={styles.copied}>{routeNote}</Text> : null}
            {notices.route !== 'discord' ? (
              <>
                <Text style={[styles.value, styles.grow]}>On this phone</Text>
                <Text style={styles.hint}>
                  {!notificationsHere
                    ? 'This device cannot show notifications; the iPhone app can.'
                    : notices.permission === 'denied'
                    ? 'Notifications are off for Expo Go: turn them on in the iPhone Settings, Expo Go, Notifications.'
                    : notices.push === 'confirmed'
                      ? 'The server sends each message here as it goes out.'
                      : `${notices.scheduled} notification${notices.scheduled === 1 ? '' : 's'} scheduled for the coming week `
                        + '(reminders, the brief, scheduled checks, the check-in, the review). They refresh each time the app opens.'}
                </Text>
                <Text style={styles.hint}>Server notifications: {pushLine(notices.push, notices.pushReason)}</Text>
                {notices.push !== 'confirmed' ? <Btn label="Try server notifications" onPress={() => void notices.retryPush()} /> : null}
              </>
            ) : null}
          </Card>
        ) : null}

        <Card label="VOICE">
          <View style={styles.row}>
            <Text style={[styles.value, styles.grow]}>Read answers to my voice messages aloud</Text>
            <Switch value={prefs.speakVoiceReplies} accessibilityLabel="Read answers to my voice messages aloud"
              onValueChange={(v) => changePrefs({ ...prefs, speakVoiceReplies: v })}
              trackColor={{ true: color.stIdle, false: color.surface3 }} />
          </View>
          <Text style={styles.hint}>Any answer can be read with its speaker button.</Text>
          <Text style={[styles.value, styles.grow]}>Speed</Text>
          <View style={styles.row}>
            {RATES.map((r) => (
              <Pressable key={r} accessibilityRole="button" accessibilityState={{ selected: prefs.rate === r }}
                onPress={() => changePrefs({ ...prefs, rate: r })} style={({ pressed }) => pressed && { opacity: 0.7 }}>
                <Glass tone={prefs.rate === r ? 'accent' : 'clear'} interactive style={styles.chip}>
                  <Text style={[styles.chipText, prefs.rate === r && styles.chipTextOn]}>{`${r}×`}</Text>
                </Glass>
              </Pressable>
            ))}
          </View>
          <Text style={styles.hint}>
            Google's Fenrir voice, French Fenrir for French. If it is unavailable, the iPhone's own voice reads.
          </Text>
        </Card>

        <Card label="ACTION BUTTON">
          <Text style={styles.body}>
            Press the Action Button, ask, and hear the answer, without opening the app. The question and answer
            land in this chat too.
          </Text>
          {SHORTCUT_STEPS.map((s, i) => (
            <View key={i} style={styles.step}>
              <Text style={styles.stepNo}>{i + 1}</Text>
              <Text style={[styles.hint, styles.grow]}>{s}</Text>
            </View>
          ))}
          {settings.baseUrl ? (
            <View style={styles.row}>
              <Btn label="Copy address" onPress={() => void copy('address')} />
              <Btn label="Copy Authorization" onPress={() => void copy('token')} />
            </View>
          ) : <Text style={styles.hint}>Pair first: the address and token come from the pairing.</Text>}
          {copied ? <Text style={styles.copied}>{copied}</Text> : null}
        </Card>

        <Card label="SHARE SHEET">
          <Text style={styles.body}>
            Send JARVIS a photo, PDF, document, link or text from any app's Share button, with a question if you like.
          </Text>
          {SHARE_STEPS.map((s, i) => (
            <View key={i} style={styles.step}>
              <Text style={styles.stepNo}>{i + 1}</Text>
              <Text style={[styles.hint, styles.grow]}>{s}</Text>
            </View>
          ))}
          {settings.baseUrl ? (
            <View style={styles.row}>
              <Btn label="Copy share address" onPress={() => void copy('share')} />
              <Btn label="Copy Authorization" onPress={() => void copy('token')} />
            </View>
          ) : <Text style={styles.hint}>Pair first: the address and token come from the pairing.</Text>}
        </Card>

        <Text style={styles.hint}>
          JARVIS app {Constants.expoConfig?.version ?? '?'} · SDK {Constants.expoConfig?.sdkVersion ?? '?'} ·{' '}
          {LIQUID_GLASS ? 'Liquid Glass on' : 'Liquid Glass unavailable here (frosted look)'}
        </Text>
        <Text style={styles.hint}>
          {describeUpdate(Constants.manifest2, __DEV__)}. A new update shows after Expo Go is closed and reopened twice.
        </Text>
      </ScrollView>
      <Scanner visible={scanning} onClose={() => setScanning(false)} onScan={(d) => void onScan(d)} />
    </View>
  );
}

function Card({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <View style={styles.card}>
      <Text style={styles.label}>{label}</Text>
      {children}
    </View>
  );
}

function Btn(p: { label: string; onPress: () => void }) {
  return (
    <Pressable accessibilityRole="button" onPress={p.onPress} style={({ pressed }) => [styles.btnWrap, pressed && { opacity: 0.75 }]}>
      <Glass tone="blue" interactive style={styles.btn}><Text style={styles.btnText}>{p.label}</Text></Glass>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.bg },
  titleRow: { flexDirection: 'row', alignItems: 'center', gap: space.s3 },
  title: { color: color.tHi, fontSize: font.lg + 4, fontWeight: '700' },
  card: {
    backgroundColor: 'rgba(14,21,33,0.72)', borderColor: color.border, borderWidth: StyleSheet.hairlineWidth,
    borderRadius: radius.lg + 6, padding: space.s4, gap: space.s2,
  },
  label: { color: color.tMid, fontSize: font.micro, letterSpacing: 2, fontWeight: '700' },
  value: { fontSize: font.body },
  body: { color: color.tHi, fontSize: font.body, lineHeight: 21 },
  hint: { color: color.tDim, fontSize: font.label, lineHeight: 19 },
  copied: { color: color.stIdle, fontSize: font.label },
  btnWrap: { flexGrow: 1 },
  btn: { minHeight: 44, borderRadius: 22, paddingHorizontal: space.s4, alignItems: 'center', justifyContent: 'center' },
  btnText: { color: color.tHi, fontSize: font.body, fontWeight: '600' },
  row: { flexDirection: 'row', alignItems: 'center', gap: space.s2 },
  grow: { flex: 1, color: color.tHi },
  step: { flexDirection: 'row', gap: space.s3, alignItems: 'flex-start' },
  stepNo: { color: color.stIdle, fontSize: font.label, fontWeight: '700', width: 14 },
  chip: { minHeight: 36, borderRadius: 18, paddingHorizontal: space.s4, justifyContent: 'center' },
  chipText: { color: color.tHi, fontSize: font.label, fontWeight: '600' },
  chipTextOn: { color: color.bg },
});
