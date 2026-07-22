/**
 * The Me tab's live notifications status (issue #210 M3) — reads the OS
 * permission on mount and again whenever the app returns to the
 * foreground (mirrors src/api/queryClient.ts's own `AppState` wiring),
 * since a landlord can flip the OS setting from Settings and come back
 * without this app ever restarting. Exposes one explicit action,
 * `requestPermission`, for the "Turn on notifications" button — never
 * called automatically (this hook only reads by itself; see
 * src/features/push/deviceRegistration.ts's `getPushPermissionState` vs
 * `requestPushPermission` split).
 */
import { useCallback, useEffect, useState } from "react";
import { AppState, type AppStateStatus } from "react-native";
import { getPushPermissionState, requestPushPermission } from "./deviceRegistration";
import type { PushPermissionState } from "./pushControl";

const INITIAL_STATE: PushPermissionState = { status: "undetermined", canAskAgain: true };

export function usePushPermission() {
  const [state, setState] = useState<PushPermissionState>(INITIAL_STATE);
  const [loading, setLoading] = useState(true);
  const [requesting, setRequesting] = useState(false);

  const refresh = useCallback(async () => {
    const next = await getPushPermissionState();
    setState(next);
    setLoading(false);
  }, []);

  useEffect(() => {
    // `refresh` setStates only AFTER awaiting the OS permission read, so
    // this is not the synchronous cascading-render pattern the rule guards
    // against — it's the allowed "seed initial state + subscribe to an
    // external system (AppState)" shape. The AppState callback below is a
    // subscription callback, exactly what the rule says setState belongs in.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refresh();
    const subscription = AppState.addEventListener("change", (next: AppStateStatus) => {
      if (next === "active") void refresh();
    });
    return () => subscription.remove();
  }, [refresh]);

  const requestPermission = useCallback(async () => {
    setRequesting(true);
    try {
      const next = await requestPushPermission();
      setState(next);
      return next;
    } finally {
      setRequesting(false);
    }
  }, []);

  return { state, loading, requesting, requestPermission };
}
