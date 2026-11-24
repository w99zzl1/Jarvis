import { useState, useCallback, useEffect, useRef } from "react";
import AetherAgent from "./components/AetherAgent";
import VoiceCore from "./components/VoiceCore";
import SettingsView from "./components/SettingsView";
import { getBackendStatus } from "./api";
import type { BackendStatus } from "./api";
import {
  applyOpacityCss,
  applyTheme,
  DEFAULT_THEME,
  getTheme,
  normalizeTheme,
  setTheme as persistTheme,
  type ThemeId,
} from "./theme";

import {
  emit,
  listen,
} from "@tauri-apps/api/event";

import {
  invoke,
} from "@tauri-apps/api/core";

// Controls that must never drag the window.
const DRAG_EXCLUDE =
  "button, input, textarea, select, a, [role='button'], [data-no-drag], [data-model-option], [data-model-selector]";

const DRAG_ICONS = "svg, img, i.material-symbols-outlined, .material-symbols-outlined";

/**
 * Empty space and panel backgrounds drag the window; anything the user can
 * read or click does not. Text nodes are checked directly (rather than relying
 * on a wrapper attribute) so every label, paragraph and number is protected
 * without having to annotate the whole tree.
 */
const shouldBlockDrag = (el: Element | null): boolean => {
  let node: Element | null = el;
  while (node && node !== document.body) {
    if (node.matches?.(DRAG_EXCLUDE) || node.closest?.(DRAG_EXCLUDE)) return true;
    if (node.closest?.(DRAG_ICONS)) return true;
    for (const child of Array.from(node.childNodes)) {
      if (child.nodeType === Node.TEXT_NODE && child.textContent?.trim()) return true;
    }
    node = node.parentElement;
  }
  return false;
};

// Global drag state to prevent focus-sync interference
let isGloballyDragging = false;

// ============================================================
// APP
// ============================================================

export default function App() {
  const params = new URLSearchParams(window.location.search);

  const isAssistantView = params.get("view") === "assistant";

  const isSettingsView = params.get("view") === "settings";

  // In the assistant window we ARE the visible assistant, so Ctrl+D there has
  // to mean "go back to the desktop", not "open myself again".
  const [isAssistantVisible, setIsAssistantVisible] = useState(isAssistantView);
  const [opacity, setOpacity] = useState(0.72);
  const [theme, setThemeState] = useState<ThemeId>(DEFAULT_THEME);

  const [isSpeaking, setIsSpeaking] = useState(false);

  // Local Python backend: Rust launches it and watches /health. A packaged
  // build has no console, so a failure has to show up here instead.
  const [backend, setBackend] = useState<BackendStatus>({
    ready: false,
    failed: false,
    message: "",
    mode: "",
  });

  // ==========================================================
  // VIEW
  // ==========================================================

  const assistantVisibleRef = useRef(isAssistantView);

  // Load persisted window appearance on mount. Rust already injected both
  // values before React painted; these effects keep React state and the CSS
  // variables in sync for the rest of the session.
  useEffect(() => {
    invoke("get_opacity")
      .then((o: number) => {
        setOpacity(o);
        applyOpacityCss(o);
      })
      .catch(() => applyOpacityCss(0.72));

    getTheme().then((t) => {
      setThemeState(t);
      applyTheme(t);
    });

    const unlistenOpacity = listen<number>("opacity-changed", (event) => {
      setOpacity(event.payload);
      applyOpacityCss(event.payload);
    });

    const unlistenTheme = listen<unknown>("theme-changed", (event) => {
      const next = normalizeTheme(event.payload);
      setThemeState(next);
      applyTheme(next);
    });

    // The assistant can also be closed from its own window, so the main
    // window has to learn about it instead of trusting its own flag.
    const unlistenAssistant = listen("assistant-visibility", (event: any) => {
      const visible = !!event.payload;
      assistantVisibleRef.current = visible;
      setIsAssistantVisible(visible);
    });

    return () => {
      unlistenOpacity.then(u => u());
      unlistenTheme.then(u => u());
      unlistenAssistant.then(u => u());
    };
  }, []);

  // Audio capture now lives in `audioCapture.ts`, owned by the window whose
  // voice button was pressed (see VoiceCore / AetherAgent).

  // Backend status: ask once on mount, then follow the Rust watchdog.
  useEffect(() => {
    getBackendStatus().then(setBackend).catch(() => {});

    const unlisten = listen<BackendStatus>("backend-status", (event: any) => {
      setBackend(event.payload);
    });

    return () => {
      unlisten.then(u => u());
    };
  }, []);

  // ==========================================================
  // CREATE / CLOSE WINDOWS
  // ==========================================================

  const toggleChatWindow = useCallback(async () => {
    const next = !isAssistantVisible;
    setIsAssistantVisible(next);
    assistantVisibleRef.current = next;
    await invoke(next ? "show_assistant" : "hide_assistant");
  }, [isAssistantVisible]);

  // ==========================================================
  // VOICE
  // ==========================================================

  // ==========================================================
  // CONNECTOR VIEW - REMOVED
  // ==========================================================

  // ==========================================================
  // UNIFIED DRAG (rigid body)
  //
  // Either window (main or assistant) can be dragged; the other
  // follows synchronously so they behave as one solid unit.
  // ==========================================================

  useEffect(() => {
    const handler = async (e: MouseEvent) => {
      if (e.button !== 0) return;
      const target = e.target as HTMLElement;

      if (shouldBlockDrag(target)) {
        return;
      }

      e.preventDefault();
      isGloballyDragging = true;
      emit("drag-start").catch(() => {});

      const startX = e.screenX;
      const startY = e.screenY;
      // Last screen position we already sent to Rust, so we send only the
      // INCREMENTAL delta each frame (no drift / no accumulation error).
      let lastSentX = startX;
      let lastSentY = startY;

      let rafId = 0;
      let lastEv: MouseEvent | null = null;

      const applyMove = () => {
        rafId = 0;
        const ev = lastEv;
        if (!ev) return;

        const incDx = ev.screenX - lastSentX;
        const incDy = ev.screenY - lastSentY;
        lastSentX = ev.screenX;
        lastSentY = ev.screenY;

        // One command -> Rust moves main + visible assistant/settings
        // atomically (DeferWindowPos). No per-window async calls.
        invoke("move_group", { dx: incDx, dy: incDy }).catch(() => {});
      };

      const onMove = (ev: MouseEvent) => {
        lastEv = ev;
        if (rafId) return;
        rafId = requestAnimationFrame(applyMove);
      };

      const onUp = () => {
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
        if (rafId) cancelAnimationFrame(rafId);
        isGloballyDragging = false;
        emit("drag-end").catch(() => {});
      };

      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    };

    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  // ==========================================================
  // FOCUS SYNC - REMOVED
  // ==========================================================


  // ==========================================================
  // GLOBAL SHORTCUT: Ctrl/Cmd + D
  //
  // Roll up the chat (assistant) the same way the
  // Assistant button does. Works while the app is focused.
  // ==========================================================

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (
        (e.ctrlKey || e.metaKey) &&
        (e.key === "d" || e.key === "D")
      ) {
        e.preventDefault();
        toggleChatWindow();
      }
    };

    window.addEventListener("keydown", onKey);

    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, [toggleChatWindow]);

  // ==========================================================
  // CONNECTOR VIEW - REMOVED
  // ==========================================================

  // ==========================================================
  // ASSISTANT VIEW
  // ==========================================================

  if (isAssistantView) {
    return (
      <>
        <style>{`
        html,
        body,
        #root {
          margin: 0 !important;
          padding: 0 !important;
          width: 100% !important;
          height: 100% !important;
          background: transparent !important;
          overflow: hidden !important;
        }
      `}</style>

        <AetherAgent
          isSpeaking={isSpeaking}
          setIsSpeaking={setIsSpeaking}
        />
      </>
    );
  }

  // ==========================================================
  // SETTINGS VIEW
  // ==========================================================

  if (isSettingsView) {
    return (
      <SettingsView
        opacity={opacity}
        theme={theme}
        onOpacityChange={(v) => {
          const rounded = Math.round(v * 100) / 100;
          void invoke("set_opacity", { opacity: rounded });
        }}
        onOpacityReset={() => void invoke("set_opacity", { opacity: 1 })}
        onThemeChange={(next) => {
          applyTheme(next);
          setThemeState(next);
          void persistTheme(next).catch(() => {});
        }}
      />
    );
  }

  // ==========================================================
  // MAIN VIEW
  // ==========================================================

  return (
    <>
      <style>{`
        html,
        body,
        #root {
          margin: 0;
          padding: 0;
          width: 100%;
          height: 100%;
          background: transparent;
          overflow: hidden;
        }

        * {
          box-sizing: border-box;
        }
      `}</style>

      <VoiceCore
        isSpeaking={isSpeaking}
        setIsSpeaking={setIsSpeaking}
        backend={backend}
      />
    </>
  );
}
