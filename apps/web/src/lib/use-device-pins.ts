"use client";

import { useCallback, useMemo, useSyncExternalStore } from "react";

const serverSnapshot = () => "[]";

/** Device-only preferences; the server and hydration both start with no pins. */
export function useDevicePins(key: "kane.agent-pins" | "kane.conversation-pins") {
  const eventName = `kane:device-pins:${key}`;
  const subscribe = useCallback((notify: () => void) => {
    const storageChanged = (event: StorageEvent) => {
      if (event.key === key || event.key === null) notify();
    };
    window.addEventListener("storage", storageChanged);
    window.addEventListener(eventName, notify);
    return () => {
      window.removeEventListener("storage", storageChanged);
      window.removeEventListener(eventName, notify);
    };
  }, [key, eventName]);
  const snapshot = useCallback(() => {
    try { return localStorage.getItem(key) || "[]"; }
    catch { return "[]"; }
  }, [key]);
  const raw = useSyncExternalStore(subscribe, snapshot, serverSnapshot);
  const pins = useMemo(() => {
    try {
      const saved: unknown = JSON.parse(raw);
      return Array.isArray(saved) ? saved.filter((id): id is string => typeof id === "string") : [];
    } catch { return []; }
  }, [raw]);
  const save = useCallback((next: string[]) => {
    // Let the existing action handlers show storage failures to the user.
    localStorage.setItem(key, JSON.stringify(next));
    window.dispatchEvent(new Event(eventName));
  }, [key, eventName]);
  return [pins, save] as const;
}
