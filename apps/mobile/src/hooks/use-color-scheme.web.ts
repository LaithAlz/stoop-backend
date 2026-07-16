import { useSyncExternalStore } from "react";
import { useColorScheme as useRNColorScheme } from "react-native";

const subscribeNoop = () => () => {};

/**
 * To support static rendering, this value needs to be re-calculated on the
 * client side for web. `useSyncExternalStore` gives a stable 'light'
 * snapshot during SSR/hydration and the real OS value once mounted, without
 * the cascading-render `useEffect`+`setState` pattern the previous
 * implementation used (flagged by react-hooks/set-state-in-effect).
 *
 * M0 ships light-only (see src/theme/tokens.ts); this hook exists so the
 * dark-mode switch in M1+ is a token swap, not a re-plumb.
 */
export function useColorScheme() {
  const hasHydrated = useSyncExternalStore(
    subscribeNoop,
    () => true,
    () => false,
  );
  const colorScheme = useRNColorScheme();

  return hasHydrated ? colorScheme : "light";
}
