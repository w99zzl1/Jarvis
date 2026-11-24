import { useState, type ReactNode } from "react";
import { normalizeTheme, THEME_OPTIONS, type ThemeId } from "../theme";

/**
 * Settings window in the Linear/Raycast desktop style from the supplied mockup.
 * Theme and opacity are live; every other control stays a visual stub.
 */

export interface SettingsViewProps {
  opacity: number;
  theme: ThemeId;
  onOpacityChange: (value: number) => void;
  onOpacityReset: () => void;
  onThemeChange: (theme: ThemeId) => void;
}

const SETTINGS_CSS = `
.set-root {
  position: fixed;
  inset: 0;
  z-index: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  box-sizing: border-box;
  overflow: hidden;
  user-select: none;
  font-family: Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  color: #d4d4d8;
  -webkit-font-smoothing: antialiased;
}
* { box-sizing: border-box; }
.set-root ::-webkit-scrollbar { width: 5px; height: 5px; }
.set-root ::-webkit-scrollbar-track { background: transparent; }
.set-root ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 9999px; }
.set-root ::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.2); }

.set-card {
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  max-width: 440px;
  overflow: hidden;
  background: rgba(12, 13, 16, 0.94);
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: 12px;
  box-shadow: 0 24px 64px rgba(0,0,0,0.8), 0 0 0 1px rgba(255,255,255,0.03);
  backdrop-filter: blur(12px);
}

/* Titlebar */
.set-titlebar {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  justify-content: space-between;
  height: 44px;
  padding: 0 16px;
  border-bottom: 1px solid rgba(255,255,255,0.06);
  background: rgba(13,14,18,0.8);
  backdrop-filter: blur(12px);
}
.set-brand { display: flex; align-items: center; gap: 10px; }
.set-brand-main { display: flex; align-items: center; gap: 8px; }
.set-dot-emerald {
  width: 8px; height: 8px; border-radius: 50%;
  background: #10b981; box-shadow: 0 0 8px rgba(16,185,129,0.7);
}
.set-mono { font-family: 'JetBrains Mono', Menlo, monospace; }
.set-brand-name { font-size: 12px; font-weight: 600; letter-spacing: 0.05em; color: #fff; }
.set-brand-sep { font-size: 11px; color: #52525b; }
.set-brand-settings { font-size: 11px; letter-spacing: -0.01em; color: #a1a1aa; }
.set-titlebar-right { display: flex; align-items: center; gap: 12px; color: #71717a; }
.set-version {
  padding: 2px 6px;
  font-size: 10px;
  letter-spacing: 0.04em;
  color: #a1a1aa;
  background: rgba(255,255,255,0.04);
  border: 1px solid rgba(255,255,255,0.06);
  border-radius: 4px;
}
.set-win-controls { display: flex; align-items: center; gap: 4px; padding-left: 4px; }
.set-win-btn {
  display: flex; align-items: center; justify-content: center;
  width: 24px; height: 24px; padding: 0;
  color: #71717a; background: none; border: 0; border-radius: 4px;
  cursor: pointer; transition: color .15s, background-color .15s;
}
.set-win-btn:hover { color: #e4e4e7; background: rgba(255,255,255,0.06); }
.set-win-btn.close:hover { color: #fb7185; background: rgba(244,63,94,0.1); }

/* Tabs */
.set-tabs {
  flex-shrink: 0;
  padding: 10px 12px 8px;
  border-bottom: 1px solid rgba(255,255,255,0.05);
  background: #0c0d10;
}
.set-tabs-inner {
  display: flex;
  gap: 4px;
  padding: 2px;
  border: 1px solid rgba(255,255,255,0.04);
  border-radius: 8px;
  background: rgba(255,255,255,0.02);
}
.set-tab {
  display: flex;
  flex: 1;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 6px 10px;
  font-family: inherit;
  font-size: 11.5px;
  font-weight: 500;
  color: #a1a1aa;
  background: transparent;
  border: 0;
  border-radius: 6px;
  cursor: pointer;
  transition: all .15s;
}
.set-tab svg { width: 14px; height: 14px; opacity: .7; }
.set-tab:hover { color: #e4e4e7; background: rgba(255,255,255,0.03); }
.set-tab.active {
  color: #fff;
  background: rgba(255,255,255,0.08);
  box-shadow: 0 1px 2px rgba(0,0,0,0.2);
}
.set-tab.active svg { opacity: .8; }

/* Content */
.set-main {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.set-group-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}
.set-group-head h3 {
  margin: 0;
  font-family: 'JetBrains Mono', Menlo, monospace;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.09em;
  text-transform: uppercase;
  color: #a1a1aa;
}
.set-group-head span {
  font-family: 'JetBrains Mono', Menlo, monospace;
  font-size: 10px;
  color: #52525b;
}
.set-group-head span.online { color: #34d399; }
.set-group-body {
  border: 1px solid rgba(255,255,255,0.05);
  border-radius: 8px;
  background: rgba(255,255,255,0.02);
  overflow: hidden;
}
.set-group-body > * + * { border-top: 1px solid rgba(255,255,255,0.04); }

.set-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px;
  cursor: default;
  transition: background-color .15s;
}
.set-row:hover { background: rgba(255,255,255,0.015); }
.set-row-copy { display: flex; flex-direction: column; padding-right: 12px; }
.set-row-title { font-size: 12.5px; font-weight: 500; color: #e4e4e7; line-height: 1.3; }
.set-row-hint { margin-top: 3px; font-size: 11px; line-height: 1.35; color: #71717a; }
.set-row-label { font-size: 12px; color: #a1a1aa; }

/* Seamless toggle */
.set-toggle-track {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  width: 40px;
  height: 22px;
  padding: 2px;
  background: #27272a;
  border: 1px solid rgba(255,255,255,0.1);
  border-radius: 9999px;
  transition: background-color .2s cubic-bezier(0.16,1,0.3,1), border-color .2s ease;
}
.set-toggle-thumb {
  width: 18px;
  height: 18px;
  background: #a1a1aa;
  border-radius: 50%;
  transition: transform .22s cubic-bezier(0.34,1.56,0.64,1);
}
.set-toggle-track.is-on { background: #10b981; border-color: #059669; }
.set-toggle-track.is-on .set-toggle-thumb {
  transform: translateX(18px);
  background: #fff;
  box-shadow: 0 1px 3px rgba(0,0,0,0.35);
}

/* Select-like stubs */
.set-select {
  display: flex;
  align-items: center;
  gap: 6px;
  max-width: 230px;
  padding: 6px 10px;
  font-family: 'JetBrains Mono', Menlo, monospace;
  font-size: 11px;
  color: #d4d4d8;
  background: rgba(255,255,255,0.04);
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: 6px;
  cursor: pointer;
  transition: background-color .15s;
}
.set-select:hover { background: rgba(255,255,255,0.07); }
.set-select-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.set-select-caret { flex-shrink: 0; width: 12px; height: 12px; color: #71717a; }
.set-select-dot { width: 6px; height: 6px; flex-shrink: 0; border-radius: 50%; }
/* Native select sits invisibly on top of the styled shell: real two-option
   picker, identical footprint and hover treatment to the other stubs. */
.set-select-wrap { position: relative; }
.set-select-native {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  margin: 0;
  opacity: 0;
  cursor: pointer;
}

/* Slider */
.set-slider-wrap { display: flex; flex-direction: column; gap: 6px; padding: 12px; }
.set-slider-head {
  display: flex;
  justify-content: space-between;
  font-family: 'JetBrains Mono', Menlo, monospace;
  font-size: 11px;
  color: #a1a1aa;
}
.set-slider-head em { font-style: normal; color: #34d399; }
.set-slider {
  width: 100%;
  height: 4px;
  margin: 0;
  border-radius: 9999px;
  appearance: none;
  background: rgba(255,255,255,0.1);
  cursor: pointer;
  accent-color: #10b981;
}
.set-slider::-webkit-slider-thumb {
  width: 12px;
  height: 12px;
  border: 0;
  border-radius: 50%;
  appearance: none;
  background: #10b981;
  box-shadow: 0 0 0 3px rgba(16,185,129,0.15);
  cursor: pointer;
}
.set-slider::-moz-range-thumb {
  width: 12px;
  height: 12px;
  border: 0;
  border-radius: 50%;
  background: #10b981;
  box-shadow: 0 0 0 3px rgba(16,185,129,0.15);
  cursor: pointer;
}

/* Keyboard shortcuts */
.set-kbd-group { display: flex; flex-direction: column; gap: 10px; padding: 12px; }
.set-kbd-row { display: flex; align-items: center; justify-content: space-between; font-size: 12px; color: #a1a1aa; }
.set-kbd {
  padding: 2px 8px;
  font-family: 'JetBrains Mono', Menlo, monospace;
  font-size: 11px;
  color: #d4d4d8;
  background: rgba(255,255,255,0.06);
  border: 1px solid rgba(255,255,255,0.1);
  border-radius: 4px;
}

/* Footer */
.set-footer {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  justify-content: space-between;
  padding: 12px;
  border-top: 1px solid rgba(255,255,255,0.06);
  background: #0c0d10;
}
.set-daemon {
  display: flex;
  align-items: center;
  gap: 8px;
  font-family: 'JetBrains Mono', Menlo, monospace;
  font-size: 11px;
  color: #71717a;
}
.set-footer-actions { display: flex; align-items: center; gap: 8px; }
.set-btn-reset {
  padding: 6px 12px;
  font-family: inherit;
  font-size: 12px;
  font-weight: 500;
  color: #a1a1aa;
  background: transparent;
  border: 0;
  border-radius: 6px;
  cursor: pointer;
  transition: color .15s, background-color .15s;
}
.set-btn-reset:hover { color: #e4e4e7; background: rgba(255,255,255,0.04); }
.set-btn-save {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  font-family: inherit;
  font-size: 12px;
  font-weight: 600;
  color: #000;
  background: #34d399;
  border: 0;
  border-radius: 6px;
  box-shadow: 0 0 12px rgba(16,185,129,0.3);
  cursor: pointer;
  transition: background-color .15s;
}
.set-btn-save:hover { background: #6ee7b7; }
.set-btn-save kbd { font-family: 'JetBrains Mono', Menlo, monospace; font-size: 10px; opacity: .6; }
`;

type TabId = "general" | "voice" | "agent";

function TabIcon({ name }: { name: TabId }) {
  if (name === "voice") {
    return (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
        <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
        <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
        <line x1="12" y1="19" x2="12" y2="23" />
        <line x1="8" y1="23" x2="16" y2="23" />
      </svg>
    );
  }
  if (name === "agent") {
    return (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="16 18 22 12 16 6" />
        <polyline points="8 6 2 12 8 18" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1.08-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1.08-1.51V3a2 2 0 0 1 4 0v.09A1.65 1.65 0 0 0 15.17 4.6a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  );
}

function Toggle({ on = false }: { on?: boolean }) {
  return (
    <span className={`set-toggle-track${on ? " is-on" : ""}`} aria-hidden="true">
      <span className="set-toggle-thumb" />
    </span>
  );
}

function SettingRow({ title, hint, on }: { title: string; hint?: string; on: boolean }) {
  return (
    <label className="set-row">
      <span className="set-row-copy">
        <span className="set-row-title">{title}</span>
        {hint && <span className="set-row-hint">{hint}</span>}
      </span>
      <Toggle on={on} />
    </label>
  );
}

function Group({ title, right, children }: { title: string; right?: string; children: ReactNode }) {
  return (
    <div className="set-group">
      <div className="set-group-head">
        <h3>{title}</h3>
        {right && <span className={right === "ONLINE" ? "online" : ""}>{right}</span>}
      </div>
      <div className="set-group-body">{children}</div>
    </div>
  );
}

function SelectStub({ text, dot, wide = false }: { text: string; dot?: string; wide?: boolean }) {
  return (
    <button type="button" className="set-select" style={wide ? { maxWidth: 220 } : undefined}>
      {dot && <span className="set-select-dot" style={{ background: dot }} />}
      <span className="set-select-text">{text}</span>
      <svg className="set-select-caret" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <path d="M19 9l-7 7-7-7" />
      </svg>
    </button>
  );
}

function ThemeSelect({ value, onChange }: { value: ThemeId; onChange: (theme: ThemeId) => void }) {
  const current = THEME_OPTIONS.find((option) => option.id === value) ?? THEME_OPTIONS[0];
  return (
    <div className="set-select set-select-wrap" title="Тема оформления">
      <span
        className="set-select-dot"
        style={{ background: value === "onyx" ? "var(--jarvis-settings-soft)" : "#34d399" }}
      />
      <span className="set-select-text">{current.label}</span>
      <svg className="set-select-caret" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <path d="M19 9l-7 7-7-7" />
      </svg>
      <select
        className="set-select-native"
        value={value}
        onChange={(e) => onChange(normalizeTheme(e.target.value))}
        aria-label="Тема оформления"
      >
        {THEME_OPTIONS.map((option) => (
          <option key={option.id} value={option.id}>{option.label}</option>
        ))}
      </select>
    </div>
  );
}

function KbdRows() {
  return (
    <div className="set-kbd-group">
      <div className="set-kbd-row"><span>Глобальный вызов окна</span><kbd className="set-kbd">Alt + Space</kbd></div>
      <div className="set-kbd-row"><span>Push-to-Talk (Голос)</span><kbd className="set-kbd">Space (Hold)</kbd></div>
      <div className="set-kbd-row"><span>Переключение Core / Voice</span><kbd className="set-kbd">Tab</kbd></div>
    </div>
  );
}

export default function SettingsView({
  opacity,
  theme,
  onOpacityChange,
  onOpacityReset,
  onThemeChange,
}: SettingsViewProps) {
  const [tab, setTab] = useState<TabId>("general");
  const tabs: { id: TabId; label: string }[] = [
    { id: "general", label: "Основные" },
    { id: "voice", label: "Голос & DSP" },
    { id: "agent", label: "AI Агент" },
  ];

  return (
    <>
      <style>{SETTINGS_CSS}</style>
      <div
        className="set-root"
        data-panel-bg
        style={{ background: `rgba(var(--jarvis-panel-rgb), ${opacity})` }}
      >
        <div className="set-card">
          <header className="set-titlebar">
            <div className="set-brand">
              <div className="set-brand-main">
                <span className="set-dot-emerald" />
                <span className="set-mono set-brand-name">AETHER</span>
              </div>
              <span className="set-mono set-brand-sep">//</span>
              <span className="set-mono set-brand-settings">SETTINGS</span>
            </div>
            <div className="set-titlebar-right">
              <span className="set-mono set-version">v2.4.1</span>
              <div className="set-win-controls">
                <button type="button" className="set-win-btn" title="Свернуть">
                  <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M3 8h10v1.2H3z" /></svg>
                </button>
                <button type="button" className="set-win-btn close" title="Закрыть">
                  <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
                    <path fillRule="evenodd" d="M3.72 3.72a.75.75 0 0 1 1.06 0L8 6.94l3.22-3.22a.75.75 0 1 1 1.06 1.06L9.06 8l3.22 3.22a.75.75 0 1 1-1.06 1.06L8 9.06l-3.22 3.22a.75.75 0 0 1-1.06-1.06L6.94 8 3.72 4.78a.75.75 0 0 1 0-1.06z" />
                  </svg>
                </button>
              </div>
            </div>
          </header>

          <nav className="set-tabs">
            <div className="set-tabs-inner">
              {tabs.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  className={`set-tab${tab === t.id ? " active" : ""}`}
                  onClick={() => setTab(t.id)}
                >
                  <TabIcon name={t.id} />
                  <span>{t.label}</span>
                </button>
              ))}
            </div>
          </nav>

          <main className="set-main">
            {tab === "general" && (
              <>
                <Group title="СИСТЕМА И ОКНО" right="RAYCAST UI">
                  <SettingRow title="Автозапуск с Windows" hint="Запуск утилиты в трей при старте системы" on />
                  <SettingRow title="Поверх всех окон (Always on Top)" hint="Фиксировать окно 400×800 во время кодинга" on />
                  <SettingRow title="Скрывать окно при потере фокуса" hint="Быстрое сворачивание как в Raycast Spotlight" on={false} />
                </Group>

                <Group title="ГРАФИКА И СТИЛЬ">
                  <SettingRow title="Аппаратное ускорение (GPU)" hint="Рендеринг анимаций через WebGL & Direct3D" on />
                  <SettingRow title="Звуковой отклик интерфейса" hint="Тихие тактильные щелчки при отправке команд" on={false} />
                  <div className="set-row">
                    <span className="set-row-copy">
                      <span className="set-row-title">Тема оформления</span>
                      <span className="set-row-hint">Палитра всех окон</span>
                    </span>
                    <ThemeSelect value={theme} onChange={onThemeChange} />
                  </div>
                  <div className="set-slider-wrap">
                    <div className="set-slider-head">
                      <span>Прозрачность окна</span>
                      <em style={{ color: "var(--jarvis-settings-accent)" }}>{Math.round(opacity * 100)}%</em>
                    </div>
                    <input
                      className="set-slider"
                      type="range"
                      min={0}
                      max={100}
                      value={Math.round(opacity * 100)}
                      onChange={(e) => onOpacityChange(Number(e.target.value) / 100)}
                    />
                  </div>
                </Group>

                <Group title="ГОРЯЧИЕ КЛАВИШИ">
                  <KbdRows />
                </Group>
              </>
            )}

            {tab === "voice" && (
              <>
                <Group title="ОБРАБОТКА РЕЧИ (DSP)" right="ONLINE">
                  <SettingRow title="Интеллектуальное шумоподавление (RNNoise)" hint="Фильтрация клавиатурных кликов и вентиляторов" on />
                  <SettingRow title="Детектор активности голоса (VAD)" hint="Автоматический старт транскрипции без нажатия кнопок" on />
                  <SettingRow title="Подавление эха (Acoustic Echo Cancellation)" hint="Предотвращает закольцовывание звука из колонок" on />
                </Group>

                <Group title="УСТРОЙСТВО ВВОДА">
                  <div className="set-row">
                    <span className="set-row-label">Микрофон:</span>
                    <SelectStub text="Realtek High Definition Audio" wide />
                  </div>
                  <div className="set-slider-wrap">
                    <div className="set-slider-head">
                      <span>Входной уровень (Gain)</span>
                      <em>82%</em>
                    </div>
                    <input className="set-slider" type="range" min={0} max={100} defaultValue={82} />
                  </div>
                </Group>
              </>
            )}

            {tab === "agent" && (
              <>
                <Group title="АВТОНОМНОСТЬ И БЕЗОПАСНОСТЬ">
                  <SettingRow title="Авто-применение патчей (Auto-Diff)" hint="Вносить изменения в код без ручного подтверждения" on={false} />
                  <SettingRow title="Подтверждать команды терминала" hint="Запрашивать разрешение перед запуском скриптов в PowerShell" on />
                  <SettingRow title="Стриминг токенов в реальном времени" hint="Посимвольный вывод ответа нейросети" on />
                </Group>

                <Group title="МОДЕЛЬ ПО УМОЛЧАНИЮ">
                  <div className="set-row">
                    <span className="set-row-label">Основной агент:</span>
                    <SelectStub text="Nvidia / Nemotron 3.5" dot="var(--jarvis-settings-soft)" wide />
                  </div>
                  <div className="set-row">
                    <span className="set-row-label">Быстрый войс-ассистент:</span>
                    <SelectStub text="gemini-2.0-flash" dot="var(--jarvis-settings-cyan)" wide />
                  </div>
                </Group>
              </>
            )}
          </main>

          <footer className="set-footer">
            <div className="set-daemon">
              <span className="set-dot-emerald" style={{ width: 6, height: 6 }} />
              <span>AETHER DAEMON READY</span>
            </div>
            <div className="set-footer-actions">
              <button type="button" className="set-btn-reset" onClick={onOpacityReset}>Сброс</button>
              <button type="button" className="set-btn-save">
                <span>Сохранить</span>
                <kbd>↵</kbd>
              </button>
            </div>
          </footer>
        </div>
      </div>
    </>
  );
}
