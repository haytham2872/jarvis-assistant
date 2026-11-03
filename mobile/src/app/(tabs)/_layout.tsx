// The native tab bar: on iOS 26+ the system draws it in Liquid Glass, tinted JARVIS blue. Chat,
// Today (his day, slice 2), Inbox (what JARVIS sent on his own, slice 4), Portfolio (slice 3) and
// Activity. Inbox counts what he has not seen; Activity, approval requests waiting on him.
import { NativeTabs } from 'expo-router/native-tabs';

import { useApp } from '../../state/AppContext';
import { color } from '../../theme/tokens';

export default function TabsLayout() {
  const { waitingCount, notices } = useApp();
  return (
    <NativeTabs tintColor={color.stIdle} minimizeBehavior="never">
      {/* The chat keeps its composer above the bar itself (an inverted list cannot take the
          automatic insets), so it opts out of them. */}
      <NativeTabs.Trigger name="index" disableAutomaticContentInsets>
        <NativeTabs.Trigger.Icon sf={{ default: 'bubble.left.and.bubble.right', selected: 'bubble.left.and.bubble.right.fill' }} />
        <NativeTabs.Trigger.Label>Chat</NativeTabs.Trigger.Label>
      </NativeTabs.Trigger>
      <NativeTabs.Trigger name="today">
        <NativeTabs.Trigger.Icon sf={{ default: 'sun.max', selected: 'sun.max.fill' }} />
        <NativeTabs.Trigger.Label>Today</NativeTabs.Trigger.Label>
      </NativeTabs.Trigger>
      <NativeTabs.Trigger name="inbox">
        <NativeTabs.Trigger.Icon sf={{ default: 'tray', selected: 'tray.fill' }} />
        <NativeTabs.Trigger.Label>Inbox</NativeTabs.Trigger.Label>
        {notices.unread ? <NativeTabs.Trigger.Badge>{String(notices.unread)}</NativeTabs.Trigger.Badge> : null}
      </NativeTabs.Trigger>
      <NativeTabs.Trigger name="portfolio">
        <NativeTabs.Trigger.Icon sf={{ default: 'chart.line.uptrend.xyaxis', selected: 'chart.line.uptrend.xyaxis' }} />
        <NativeTabs.Trigger.Label>Portfolio</NativeTabs.Trigger.Label>
      </NativeTabs.Trigger>
      <NativeTabs.Trigger name="activity">
        <NativeTabs.Trigger.Icon sf={{ default: 'square.stack.3d.up', selected: 'square.stack.3d.up.fill' }} />
        <NativeTabs.Trigger.Label>Activity</NativeTabs.Trigger.Label>
        {waitingCount ? <NativeTabs.Trigger.Badge>{String(waitingCount)}</NativeTabs.Trigger.Badge> : null}
      </NativeTabs.Trigger>
    </NativeTabs>
  );
}
