import { invoke } from "@tauri-apps/api/core";

export type ThemeId = "aether" | "onyx";

export interface ThemeOption {
  id: ThemeId;
  label: string;
}

export const THEME_OPTIONS: ThemeOption[] = [
  { id: "aether", label: "Aether Voice" },
  { id: "onyx", label: "Onyx Dark" },
];

export const DEFAULT_THEME: ThemeId = "aether";

export function normalizeTheme(raw: unknown): ThemeId {
  return raw === "onyx" ? "onyx" : DEFAULT_THEME;
}

/** Applies the palette to the current WebView window. Layout is untouched. */
export function applyTheme(theme: ThemeId): void {
  document.documentElement.dataset.theme = theme;
}

/** Keeps the translucent window background in sync across every window. */
export function applyOpacityCss(opacity: number): void {
  document.documentElement.style.setProperty("--jarvis-opacity", String(opacity));
}

export async function getTheme(): Promise<ThemeId> {
  try {
    return normalizeTheme(await invoke("get_theme"));
  } catch {
    // Plain browser dev (no Tauri shell): use the built-in palette.
    return DEFAULT_THEME;
  }
}

export async function setTheme(theme: ThemeId): Promise<void> {
  await invoke("set_theme", { theme });
}
