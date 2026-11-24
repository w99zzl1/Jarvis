import React, { useCallback, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { fetchLiveModels } from "../api";
import {
  PROVIDER_ENV_HINT,
  PROVIDER_LABELS,
  classifyModel,
  type ModelTier,
  type ProviderId,
} from "../models";

/**
 * AETHER "Select AI Intelligence Model" dialog.
 *
 * Ported from the AETHER design (assistant design mockup) — header, tier tab
 * strip, scrollable model list, Cancel / Apply Model footer. Models come from
 * the live backend catalog (`POST /models/fetch`) grouped by provider; the
 * selection is staged locally and only persisted on "Apply Model".
 */
interface AetherModelModalProps {
  primaryModel: { provider: ProviderId; model: string } | null;
  onPrimaryChange: (spec: { provider: ProviderId; model: string }) => void;
  apiKeys: Record<string, string>;
  /** Persist a new provider key; changing `apiKeys` re-fetches the whole catalog. */
  onConnectProvider: (provider: ProviderId, key: string) => void | Promise<void>;
  onClose: () => void;
}

type TabId = "recommended" | "fast" | "reasoning";

/** Providers a user can attach a key to from inside the picker. */
const CONNECTABLE_PROVIDERS: ProviderId[] = ["google", "groq", "nim"];

const TABS: { id: TabId; label: string }[] = [
  { id: "recommended", label: "Recommended" },
  { id: "fast", label: "Fast / Low Latency" },
  { id: "reasoning", label: "Reasoning & Coding" },
];

/** Tier a tab narrows to; `null` keeps every tier. */
const TAB_TIER: Record<TabId, ModelTier | null> = {
  recommended: null,
  fast: "fast",
  reasoning: "smart",
};

interface ModelRow {
  key: string;
  provider: ProviderId;
  model: string;
  /** Total models exposed by this provider (unfiltered), as in the design. */
  variants: number;
}

export default function AetherModelModal({
  primaryModel,
  onPrimaryChange,
  apiKeys,
  onConnectProvider,
  onClose,
}: AetherModelModalProps) {
  const [tab, setTab] = useState<TabId>("recommended");
  const [models, setModels] = useState<Record<string, string[]>>({});
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState<{ provider: ProviderId; model: string } | null>(
    primaryModel,
  );

  // --- inline "connect provider" form -------------------------------------
  const [connectOpen, setConnectOpen] = useState(false);
  const [connectProvider, setConnectProvider] = useState<ProviderId>("google");
  const [connectKey, setConnectKey] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [connectError, setConnectError] = useState("");

  useEffect(() => {
    let alive = true;
    setLoading(true);
    fetchLiveModels(apiKeys)
      .then((res) => {
        if (alive) setModels(res || {});
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [apiKeys]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const rows = useMemo(() => {
    const tier = TAB_TIER[tab];
    const out: ModelRow[] = [];
    for (const [provider, list] of Object.entries(models)) {
      for (const model of list) {
        if (tier && classifyModel(model) !== tier) continue;
        out.push({
          key: `${provider}:${model}`,
          provider: provider as ProviderId,
          model,
          variants: list.length,
        });
      }
    }
    return out;
  }, [models, tab]);

  const connectedProviders = useMemo(
    () =>
      CONNECTABLE_PROVIDERS.filter((p) => Boolean(apiKeys[p])).map(
        (p) => PROVIDER_LABELS[p] || p,
      ),
    [apiKeys],
  );

  /** Save the key to config — `apiKeys` changes, so the catalog re-fetches below. */
  const connect = useCallback(async () => {
    const key = connectKey.trim();
    if (!key) {
      setConnectError("Введите API-ключ");
      return;
    }
    setConnecting(true);
    setConnectError("");
    try {
      await onConnectProvider(connectProvider, key);
      setConnectKey("");
      setConnectError("");
      setConnectOpen(false);
    } catch (e: any) {
      setConnectError(e?.message || String(e));
    } finally {
      setConnecting(false);
    }
  }, [connectKey, connectProvider, onConnectProvider]);

  const apply = useCallback(() => {
    if (
      pending &&
      (!primaryModel ||
        pending.provider !== primaryModel.provider ||
        pending.model !== primaryModel.model)
    ) {
      onPrimaryChange(pending);
    }
    onClose();
  }, [pending, primaryModel, onPrimaryChange, onClose]);

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-[#090A0C]/80 p-4 backdrop-blur-sm"
      onClick={onClose}
      data-no-drag
      data-aether
      data-model-modal
    >
      <div
        className="flex max-h-[85vh] w-full max-w-lg flex-col overflow-hidden rounded-xl border border-[#22252C] bg-[#0E1013] shadow-[0_25px_50px_-12px_rgba(0,0,0,0.35),inset_0_1px_0_0_rgba(255,255,255,0.06)]"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex shrink-0 items-center justify-between border-b border-[#22252C] bg-[#14161B] px-4 py-3">
          <div className="flex items-center gap-2">
            <span className="h-2 w-2 rounded-full bg-purple-400" />
            <h3 className="font-mono text-xs font-semibold uppercase tracking-wider text-[#ECEEF2]">
              Select AI Intelligence Model
            </h3>
          </div>
          <button
            type="button"
            onClick={onClose}
            title="Close"
            className="font-mono text-sm text-[#6E7480] transition hover:text-[#ECEEF2]"
          >
            ✕
          </button>
        </div>

        {/* Body */}
        <div className="flex min-h-0 flex-1 flex-col gap-3 p-3.5 font-mono">
          <div className="flex shrink-0 items-center gap-1 border-b border-[#22252C] pb-2 text-[11px]">
            {TABS.map((t) => (
              <button
                key={t.id}
                type="button"
                onClick={() => setTab(t.id)}
                className={`rounded px-2 py-1 transition ${
                  tab === t.id
                    ? "border border-[#22252C] bg-[#14161B] text-[#ECEEF2]"
                    : "text-[#8A8F98] hover:text-[#ECEEF2]"
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>

          {/* --- Connect a provider inline ---------------------------------- */}
          <div className="flex shrink-0 items-center justify-between gap-2">
            <span className="truncate text-[10px] text-[#6E7480]">
              {connectedProviders.length
                ? `Подключено: ${connectedProviders.join(", ")}`
                : "Нет подключённых провайдеров"}
            </span>
            <button
              type="button"
              onClick={() => {
                setConnectOpen((v) => !v);
                setConnectError("");
              }}
              className="shrink-0 rounded border border-[#22252C] bg-[#14161B] px-2 py-1 text-[10px] text-[#ECEEF2] transition hover:border-emerald-500/40 hover:text-emerald-400"
            >
              {connectOpen ? "− Скрыть" : "+ Подключить провайдера"}
            </button>
          </div>

          {connectOpen && (
            <div className="shrink-0 space-y-2 rounded-lg border border-[#22252C] bg-[#090A0C] p-2.5">
              <div className="flex items-center gap-1">
                {CONNECTABLE_PROVIDERS.map((p) => (
                  <button
                    key={p}
                    type="button"
                    onClick={() => {
                      setConnectProvider(p);
                      setConnectError("");
                    }}
                    className={`flex items-center gap-1.5 rounded px-2 py-1 text-[10px] transition ${
                      connectProvider === p
                        ? "border border-emerald-500/30 bg-[#14161B] text-emerald-400"
                        : "border border-[#22252C] text-[#8A8F98] hover:text-[#ECEEF2]"
                    }`}
                  >
                    <span
                      className={`h-1.5 w-1.5 rounded-full ${
                        apiKeys[p] ? "bg-emerald-400" : "bg-[#3A4150]"
                      }`}
                    />
                    {PROVIDER_LABELS[p] || p}
                  </button>
                ))}
              </div>

              <input
                type="password"
                value={connectKey}
                onChange={(e) => setConnectKey(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void connect();
                  }
                }}
                placeholder={PROVIDER_ENV_HINT[connectProvider]}
                autoFocus
                className="w-full rounded border border-[#22252C] bg-[#14161B] px-2 py-1.5 text-[11px] text-[#ECEEF2] placeholder-[#555B66] outline-none transition focus:border-emerald-500/50"
              />

              {connectError && (
                <div className="text-[10px] text-rose-400">{connectError}</div>
              )}

              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => {
                    setConnectOpen(false);
                    setConnectKey("");
                    setConnectError("");
                  }}
                  className="rounded px-2 py-1 text-[10px] text-[#8A8F98] transition hover:bg-[#1B1E24] hover:text-[#ECEEF2]"
                >
                  Отмена
                </button>
                <button
                  type="button"
                  onClick={() => void connect()}
                  disabled={connecting || !connectKey.trim()}
                  className="rounded bg-emerald-500 px-2.5 py-1 text-[10px] font-semibold text-[#07130D] transition hover:bg-emerald-400 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {connecting ? "Подключаю…" : "Подключить"}
                </button>
              </div>
            </div>
          )}

          <div className="min-h-0 flex-1 space-y-1.5 overflow-y-auto text-xs">
            {loading ? (
              <div className="py-10 text-center text-[#6E7480]">Loading models…</div>
            ) : rows.length === 0 ? (
              <div className="py-10 text-center text-[#6E7480]">
                Нет моделей — подключите провайдера и введите API-ключ выше.
              </div>
            ) : (
              rows.map((r) => {
                const active =
                  primaryModel?.provider === r.provider && primaryModel?.model === r.model;
                const selected =
                  pending?.provider === r.provider && pending?.model === r.model;
                return (
                  <button
                    key={r.key}
                    type="button"
                    onClick={() => setPending({ provider: r.provider, model: r.model })}
                    className={`flex w-full cursor-pointer items-center justify-between rounded-lg border p-2.5 text-left transition ${
                      selected
                        ? "border-purple-500/40 bg-[#14161B]"
                        : "border-[#22252C] bg-[#090A0C] hover:border-[#4A505E] hover:bg-[#14161B]"
                    }`}
                  >
                    <span className="min-w-0">
                      <span className="flex items-center gap-2">
                        <span className="truncate font-semibold text-[#ECEEF2]">{r.model}</span>
                        {active && (
                          <span className="shrink-0 rounded border border-purple-500/20 bg-purple-500/10 px-1.5 py-0.5 text-[10px] text-purple-400">
                            ACTIVE
                          </span>
                        )}
                      </span>
                      <span className="mt-0.5 block text-[11px] text-[#6E7480]">
                        {PROVIDER_LABELS[r.provider] || r.provider} · {r.variants} variants
                      </span>
                    </span>
                    <span className="shrink-0 pl-3 text-right text-[11px]">
                      {selected ? (
                        <span className="text-emerald-400">●</span>
                      ) : (
                        <span className="text-[#6E7480]">—</span>
                      )}
                    </span>
                  </button>
                );
              })
            )}
          </div>
        </div>

        {/* Footer */}
        <div className="flex shrink-0 items-center justify-end gap-2 border-t border-[#22252C] bg-[#14161B] px-4 py-2.5 font-mono text-xs">
          <button
            type="button"
            onClick={onClose}
            className="rounded px-3 py-1 text-[#8A8F98] transition hover:bg-[#1B1E24] hover:text-[#ECEEF2]"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={apply}
            disabled={!pending}
            className="rounded bg-[#ECEEF2] px-3 py-1 font-semibold text-[#090A0C] transition hover:bg-white disabled:cursor-not-allowed disabled:opacity-50"
          >
            Apply Model
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
