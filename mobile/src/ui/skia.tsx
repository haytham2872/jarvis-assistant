import { Component, type ReactNode } from 'react';
import { Platform, TurboModuleRegistry } from 'react-native';

/** Probe before importing a drawing component: absent native modules can throw on import. */
export function skiaAvailable() {
  if (Platform.OS === 'web') return false;
  try {
    return ['RNSkiaModule', 'WorkletsModule', 'ReanimatedModule'].every((name) => !!TurboModuleRegistry.get(name));
  } catch { return false; }
}

export class SkiaBoundary extends Component<{ fallback: ReactNode; children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch(error: Error) { console.warn('Native drawing failed; showing its fallback.', error); }
  render() { return this.state.failed ? this.props.fallback : this.props.children; }
}
