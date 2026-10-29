import React, { useState } from "react";

// ============================================================
// INLINE
// ============================================================

const KEYWORDS = new Set([
  "import", "from", "export", "const", "let", "var", "return", "function",
  "async", "await", "if", "else", "for", "while", "new", "class", "extends",
  "def", "self", "try", "except", "catch", "finally", "throw", "type", "interface",
  "true", "false", "null", "None", "True", "False", "undefined", "public",
  "private", "static", "void", "in", "of", "not", "and", "or", "yield",
]);

const CODE_CYAN = "text-primary";
const CODE_VIOLET = "text-secondary-fixed-dim";
const CODE_DIM = "text-outline";

function highlight(line: string, keyBase: string): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  const token =
    /(\/\/[^\n]*|#[^\n]*)|('(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_$][\w$]*)|([{}()[\];:,.<>=+\-*/%!&|?])/g;

  let last = 0;
  let i = 0;
  let m: RegExpExecArray | null;

  while ((m = token.exec(line)) !== null) {
    if (m.index > last) nodes.push(line.slice(last, m.index));
    const [full, comment, str, num, word, punct] = m;

    if (comment) {
      nodes.push(
        <span key={`${keyBase}-c${i}`} className={CODE_DIM}>
          {full}
        </span>,
      );
    } else if (str) {
      nodes.push(
        <span key={`${keyBase}-s${i}`} className={CODE_CYAN}>
          {full}
        </span>,
      );
    } else if (num) {
      nodes.push(
        <span key={`${keyBase}-n${i}`} className="text-secondary">
          {full}
        </span>,
      );
    } else if (word) {
      nodes.push(
        KEYWORDS.has(word) ? (
          <span key={`${keyBase}-k${i}`} className={CODE_VIOLET}>
            {full}
          </span>
        ) : (
          <span key={`${keyBase}-w${i}`}>{full}</span>
        ),
      );
    } else if (punct) {
      nodes.push(
        <span key={`${keyBase}-p${i}`} className="text-outline">
          {full}
        </span>,
      );
    }
    last = m.index + full.length;
    i++;
  }

  if (last < line.length) nodes.push(line.slice(last));
  return nodes;
}

function renderInline(text: string, keyPrefix: string): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  const regex = /(\*\*([^*]+)\*\*|`([^`]+)`)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = regex.exec(text)) !== null) {
    if (m.index > last) nodes.push(text.slice(last, m.index));
    if (m[2] !== undefined) {
      nodes.push(
        <strong
          key={`${keyPrefix}-b${i}`}
          className="font-semibold text-white"
        >
          {m[2]}
        </strong>,
      );
    } else if (m[3] !== undefined) {
      nodes.push(
        <code
          key={`${keyPrefix}-i${i}`}
          className="rounded bg-surface-container-lowest/80 px-1.5 py-0.5 font-label-sm text-label-sm text-primary-fixed-dim"
        >
          {m[3]}
        </code>,
      );
    }
    last = m.index + m[0].length;
    i++;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

// ============================================================
// CODE BLOCK
// ============================================================

function CodeBlock({ code, lang }: { code: string; lang: string }) {
  const [copied, setCopied] = useState(false);

  const copy = () => {
    navigator.clipboard?.writeText(code).then(
      () => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      },
      () => {},
    );
  };

  return (
    <div className="flex w-full flex-col overflow-hidden rounded-lg bg-surface-container-lowest shadow-inner">
      <div className="flex items-center justify-between bg-surface-container-high/60 px-space-md py-2">
        <div className="flex items-center gap-space-sm">
          <span className="h-2.5 w-2.5 rounded-full bg-error/70" />
          <span className="h-2.5 w-2.5 rounded-full bg-secondary-fixed/50" />
          <span className="h-2.5 w-2.5 rounded-full bg-primary-container/70" />
          <span className="ml-2 font-label-sm text-label-sm text-on-surface-variant">
            {lang ? `${lang} snippet` : "code snippet"}
          </span>
        </div>
        <button
          type="button"
          onClick={copy}
          className="flex items-center gap-1 rounded bg-surface-container-highest/80 px-2 py-0.5 text-on-surface transition-all hover:bg-primary-container hover:text-on-primary-container"
        >
          <span className="material-symbols-outlined text-[14px]">
            {copied ? "check" : "content_copy"}
          </span>
          <span className="font-label-sm text-label-sm uppercase">
            {copied ? "Copied" : "Copy Code"}
          </span>
        </button>
      </div>
      <pre className="overflow-x-auto p-space-md font-label-md text-label-md leading-relaxed text-on-surface selection:bg-primary-container selection:text-on-primary-container">
        <code>
          {code.split("\n").map((line, li) => (
            <React.Fragment key={li}>
              {highlight(line, `l${li}`)}
              {li < code.split("\n").length - 1 ? "\n" : ""}
            </React.Fragment>
          ))}
        </code>
      </pre>
    </div>
  );
}

// ============================================================
// MARKDOWN
// ============================================================

export default function Markdown({ text }: { text: string }) {
  const blocks = text.split("```");

  return (
    <div className="flex flex-col gap-space-sm">
      {blocks.map((block, idx) => {
        if (idx % 2 === 1) {
          const nl = block.indexOf("\n");
          const rawLang = nl >= 0 ? block.slice(0, nl).trim() : "";
          const code = (nl >= 0 ? block.slice(nl + 1) : block).replace(/\n$/, "");
          return <CodeBlock key={idx} code={code} lang={rawLang} />;
        }

        const lines = block.split("\n");
        return (
          <React.Fragment key={idx}>
            {lines.map((line, li) => {
              const h = /^(#{1,4})\s+(.*)$/.exec(line);
              if (h) {
                const level = h[1].length;
                return (
                  <div
                    key={li}
                    className={`font-headline-sm tracking-tight text-primary ${
                      level === 1 ? "text-headline-md" : "text-headline-sm"
                    }`}
                  >
                    {renderInline(h[2], `l${idx}-${li}`)}
                  </div>
                );
              }

              const bullet = /^(\s*)([-*+]|\d+\.)\s+(.*)$/.exec(line);
              if (bullet) {
                return (
                  <div key={li} className="flex gap-2 font-body-md text-body-md text-on-surface">
                    <span className="text-primary-container">{bullet[2]}</span>
                    <span className="flex-1 whitespace-pre-wrap break-words">
                      {renderInline(bullet[3], `b${idx}-${li}`)}
                    </span>
                  </div>
                );
              }

              if (!line.trim()) return <div key={li} className="h-1" />;

              return (
                <p
                  key={li}
                  className="whitespace-pre-wrap break-words font-body-md text-body-md leading-relaxed text-on-surface"
                >
                  {renderInline(line, `p${idx}-${li}`)}
                </p>
              );
            })}
          </React.Fragment>
        );
      })}
    </div>
  );
}