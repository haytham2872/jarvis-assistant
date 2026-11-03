// Full-screen QR scanner for the pairing code (expo-camera, included in Expo Go).
import { CameraView, useCameraPermissions } from 'expo-camera';
import { useRef } from 'react';
import { Modal, Pressable, StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { color, font, radius, space } from '../theme/tokens';

export function Scanner({ visible, onScan, onClose }: {
  visible: boolean;
  onScan: (data: string) => void;
  onClose: () => void;
}) {
  const [permission, requestPermission] = useCameraPermissions();
  const insets = useSafeAreaInsets();
  const handled = useRef(false);   // one scan per opening: the camera reports a code many times a second

  return (
    <Modal visible={visible} animationType="slide" onShow={() => { handled.current = false; }} onRequestClose={onClose}>
      <View style={[styles.root, { paddingTop: insets.top + space.s3, paddingBottom: insets.bottom + space.s3 }]}>
        <Text style={styles.title}>Scan the pairing QR on the PC</Text>
        {permission?.granted ? (
          <CameraView
            style={styles.camera}
            facing="back"
            barcodeScannerSettings={{ barcodeTypes: ['qr'] }}
            onBarcodeScanned={({ data }) => {
              if (handled.current) return;
              handled.current = true;
              onScan(data);
            }}
          />
        ) : (
          <View style={[styles.camera, styles.center]}>
            <Text style={styles.text}>
              {permission?.canAskAgain === false
                ? 'Camera access is off: iPhone Settings › Expo Go › Camera.'
                : 'JARVIS needs the camera to read the pairing code.'}
            </Text>
            {permission?.canAskAgain !== false && (
              <Pressable accessibilityRole="button" style={styles.btn} onPress={requestPermission}>
                <Text style={styles.btnText}>Allow camera</Text>
              </Pressable>
            )}
          </View>
        )}
        <Pressable accessibilityRole="button" style={styles.btn} onPress={onClose}>
          <Text style={styles.btnText}>Cancel</Text>
        </Pressable>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: color.bg, paddingHorizontal: space.s4, gap: space.s3 },
  title: { color: color.tHi, fontSize: font.read },
  camera: { flex: 1, borderRadius: radius.lg, overflow: 'hidden', backgroundColor: color.surface },
  center: { alignItems: 'center', justifyContent: 'center', padding: space.s4, gap: space.s3 },
  text: { color: color.tMid, fontSize: font.body, textAlign: 'center' },
  btn: {
    minHeight: 48, borderRadius: radius.md, alignItems: 'center', justifyContent: 'center',
    backgroundColor: color.surface3, borderColor: color.borderBright, borderWidth: StyleSheet.hairlineWidth,
  },
  btnText: { color: color.tHi, fontSize: font.body },
});
