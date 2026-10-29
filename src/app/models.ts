/**
 * Каталог доступных моделей.
 * Каждая модель жёстко связана с provider.
 * Нигде больше нет эвристики / парсинга имени — только этот каталог.
 */

export type ProviderId =
  | "groq"
  | "google"
  | "openai"
  | "openrouter"
  | "nim"
  | "openai_compat";

export interface ModelOption {
  /** Идентификатор провайдера — отправляется на бэкенд как есть */
  provider: ProviderId;
  /** Строка модели для SDK — отправляется на бэкенд как есть */
  model: string;
  /** Читаемое название для UI селекта */
  label: string;
  /** Группа (чтобы разделить в dropdown) */
  group: string;
  /** Уровень (cheap / pro) — пометка в UI */
  tier?: ModelTier;
}

/**
 * Класс модели. Бэкенд уже отбросил всё, что не умеет чат (image/tts/embed),
 * поэтому здесь осталось разделить именно чат-модели по назначению.
 */
export type ModelTier = "fast" | "smart" | "preview";

export const TIER_LABELS: Record<ModelTier, string> = {
  fast: "быстрая / дешёвая",
  smart: "умная / мощная",
  preview: "preview (нестабильная)",
};

const PREVIEW_RE = /(^|[-_/])(preview|exp|experimental|latest)(\b|[-_/])/i;
const FAST_RE = /(flash|mini|nano|lite|turbo|haiku|small|8b|7b|4b|3b|1b|2b|gemma|phi-|nemotron-.*-(nano|mini))/i;
const SMART_RE = /(pro|opus|sonnet|max|ultra|premium|405b|70b|120b|gpt-4o|gpt-4\.1|gpt-5|^o1|^o3|^o4|deepseek-r1|qwq)/i;

/**
 * Определяет класс модели по её id. Только для подписи в UI — на бэкенд
 * уходит ровно та строка, которую вернул провайдер.
 */
export function classifyModel(model: string): ModelTier {
  if (PREVIEW_RE.test(model)) return "preview";
  if (SMART_RE.test(model)) return "smart";
  if (FAST_RE.test(model)) return "fast";
  return "smart";
}

export const PROVIDER_LABELS: Record<ProviderId, string> = {
  groq: "Groq",
  google: "Google",
  openai: "OpenAI",
  openrouter: "OpenRouter",
  nim: "NVIDIA NIM",
  openai_compat: "Self-hosted",
};

export const PROVIDER_ENV_HINT: Record<ProviderId, string> = {
  groq: "GROQ_API_KEY",
  google: "GOOGLE_API_KEY (AIza...)",
  openai: "OPENAI_API_KEY (sk-...)",
  openrouter: "OPENROUTER_API_KEY (sk-or-...)",
  nim: "NIM_API_KEY (nvapi-...)",
  openai_compat: "API Key self-hosted endpoint",
};

export const MODEL_CATALOG: ModelOption[] = [];

/** Найти опцию модели по (provider, model). Иначе — вернуть дефолтную сущность */
export function findModelOption(
  provider: string | null | undefined,
  model: string | null | undefined,
): ModelOption | undefined {
  if (!provider || !model) return undefined;
  return {
    provider: provider as ProviderId,
    model,
    label: model,
    group: PROVIDER_LABELS[provider as ProviderId] || provider,
  };
}

/** Сформировать уникальный ключ опции, чтобы React list работали правильно */
export function modelKey(opt: { provider: string; model: string }): string {
  return `${opt.provider}:${opt.model}`;
}

/** Для primary — быстрый дефолт если пользователь ещё ничего не выбрал */
export const DEFAULT_PRIMARY: ModelOption = {
  provider: "groq",
  model: "",
  label: "",
  group: "Groq",
};


