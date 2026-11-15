import type { Message } from "./api";

/** Объединяет историю и события ленты: без дублей, по порядку журнала, не больше 300. */
export function mergeMessages(list: Message[], incoming: Message[]): Message[] {
  const byId = new Map(list.map((m) => [m.id, m]));
  for (const m of incoming) byId.set(m.id, m);
  return [...byId.values()].sort((a, b) => a.id - b.id).slice(-300);
}
