/**
 * EmergencyBanner's acknowledge affordance (api-contracts.md's Queue
 * section v1.15 amendment). Zero network — this is a pure render test of
 * the presentational component; the mutation itself is covered by
 * src/features/emergency/__tests__/useAcknowledge.test.tsx and the
 * `notification_id` gate by emergencyBanner.test.ts's
 * `hasAcknowledgeableNotification` cases.
 *
 * The two things a safety reviewer should check here: (1) the ack button
 * only ever exists when the caller passes `onAcknowledge` (case-detail
 * never does — see that screen's own comment), and (2) tapping it never
 * also fires `onPress` (open-case) — a nested Pressable that leaked its
 * tap to the parent would silently navigate away mid-acknowledge.
 */
import { fireEvent, render, screen } from "@testing-library/react-native";
import { EmergencyBanner } from "../EmergencyBanner";
import {
  EMERGENCY_ACK_LABEL,
  EMERGENCY_ACK_PENDING_LABEL,
  EMERGENCY_ACK_SUBLABEL,
} from "@/features/emergency/emergencyBanner";

describe("EmergencyBanner — no onAcknowledge prop (case-detail's usage today)", () => {
  it("renders exactly the informational banner — no ack button at all", () => {
    render(
      <EmergencyBanner
        headline="Maria needs you now — 41 Palmerston"
        subtext="41 Palmerston · tap to see what's happening"
        onPress={jest.fn()}
      />,
    );

    expect(screen.getByText("Maria needs you now — 41 Palmerston")).toBeOnTheScreen();
    expect(screen.queryByTestId("emergency-ack-button")).toBeNull();
    expect(screen.queryByText(EMERGENCY_ACK_LABEL)).toBeNull();
  });

  it("tapping the banner still opens the case", () => {
    const onPress = jest.fn();
    render(
      <EmergencyBanner
        headline="Maria needs you now — 41 Palmerston"
        subtext="41 Palmerston · tap to see what's happening"
        onPress={onPress}
      />,
    );

    fireEvent.press(screen.getByText("Maria needs you now — 41 Palmerston"));
    expect(onPress).toHaveBeenCalledTimes(1);
  });
});

describe("EmergencyBanner — onAcknowledge provided (Home's usage for a non-null notification_id)", () => {
  it("shows the ack button with the house copy", () => {
    render(
      <EmergencyBanner
        headline="Maria needs you now — 41 Palmerston"
        subtext="41 Palmerston · tap to see what's happening"
        onPress={jest.fn()}
        onAcknowledge={jest.fn()}
      />,
    );

    expect(screen.getByTestId("emergency-ack-button")).toBeOnTheScreen();
    expect(screen.getByText(EMERGENCY_ACK_LABEL)).toBeOnTheScreen();
    expect(screen.getByText(EMERGENCY_ACK_SUBLABEL)).toBeOnTheScreen();
  });

  it("tapping the ack button calls onAcknowledge, not onPress", () => {
    const onPress = jest.fn();
    const onAcknowledge = jest.fn();
    render(
      <EmergencyBanner
        headline="Maria needs you now — 41 Palmerston"
        subtext="41 Palmerston · tap to see what's happening"
        onPress={onPress}
        onAcknowledge={onAcknowledge}
      />,
    );

    fireEvent.press(screen.getByTestId("emergency-ack-button"));

    expect(onAcknowledge).toHaveBeenCalledTimes(1);
    expect(onPress).not.toHaveBeenCalled();
  });

  it("tapping the headline row still calls onPress, not onAcknowledge", () => {
    const onPress = jest.fn();
    const onAcknowledge = jest.fn();
    render(
      <EmergencyBanner
        headline="Maria needs you now — 41 Palmerston"
        subtext="41 Palmerston · tap to see what's happening"
        onPress={onPress}
        onAcknowledge={onAcknowledge}
      />,
    );

    fireEvent.press(screen.getByText("Maria needs you now — 41 Palmerston"));

    expect(onPress).toHaveBeenCalledTimes(1);
    expect(onAcknowledge).not.toHaveBeenCalled();
  });

  it("shows the pending label while acknowledging, never the idle label", () => {
    render(
      <EmergencyBanner
        headline="Maria needs you now — 41 Palmerston"
        subtext="41 Palmerston · tap to see what's happening"
        onPress={jest.fn()}
        onAcknowledge={jest.fn()}
        acknowledging
      />,
    );

    expect(screen.getByText(EMERGENCY_ACK_PENDING_LABEL)).toBeOnTheScreen();
    expect(screen.queryByText(EMERGENCY_ACK_LABEL)).toBeNull();
  });
});
