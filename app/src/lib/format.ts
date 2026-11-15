const CHANNELS: Record<string, string> = {
  telegram: "Telegram",
  voice: "голос",
  app: "приложение",
  api: "API",
  station: "Станция",
};

export const channelName = (c: string | null | undefined): string => (c ? CHANNELS[c] ?? c : "");

export function time(ts: number): string {
  const d = new Date(ts * 1000);
  const today = new Date().toDateString() === d.toDateString();
  return d.toLocaleString("ru-RU", today ? { hour: "2-digit", minute: "2-digit" } : {
    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  });
}

const KINDS: Record<string, string> = {
  user: "Вы",
  assistant: "Ответ",
  tool: "Инструмент",
  denied: "Отказано",
  error: "Ошибка",
  confirm_asked: "Запрос подтверждения",
  confirm_resolved: "Решение",
};
export const kindName = (k: string): string => KINDS[k] ?? k;

export const toolName = (n: string): string => n.replace(/^mcp__jarvis__/, "").replace(/^mcp__/, "");

const VAULT_DIRS = /\/(00_Inbox|10_Projects|20_Areas|30_Decisions|40_Journal|90_Agent)\//;

/** Путь внутри vault — от папки vault; прочие — последние две части. */
export function shortPath(p: string): string {
  const norm = p.replace(/\\/g, "/");
  const m = norm.match(VAULT_DIRS);
  if (m && m.index !== undefined) return norm.slice(m.index + 1);
  const parts = norm.split("/").filter(Boolean);
  return parts.length > 2 ? "…/" + parts.slice(-2).join("/") : norm;
}
