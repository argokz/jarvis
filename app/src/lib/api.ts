import { type Conn, httpUrl } from "./config";

export interface Message {
  id: number;
  ts: number;
  channel: string;
  role: "user" | "assistant";
  text: string;
}
export interface JournalEvent {
  type: "journal";
  id: number;
  ts: number;
  channel: string;
  kind: string;
  name: string;
  text: string;
}
export interface Confirmation {
  id: string;
  title: string;
  details: string;
  channel: string;
  created: number;
  expires: number;
}
export type Project = Record<string, unknown> & {
  id: string;
  status: string;
  _file: string;
  _open_tasks?: number;
};
export interface ChatReply {
  text: string;
  is_error: boolean;
  cost_usd: number | null;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export class Api {
  constructor(private conn: () => Conn) {}

  private async req<T>(path: string, init: RequestInit = {}): Promise<T> {
    const c = this.conn();
    let r: Response;
    try {
      r = await fetch(httpUrl(c, path), {
        ...init,
        headers: { Authorization: `Bearer ${c.token}`, "Content-Type": "application/json" },
      });
    } catch (e) {
      throw new ApiError(0, `Сервер недоступен: ${c.server}`);
    }
    if (!r.ok) {
      let detail = await r.text();
      try {
        detail = JSON.parse(detail).detail ?? detail;
      } catch {
        /* текст как есть */
      }
      throw new ApiError(r.status, r.status === 401 ? "Неверный токен" : String(detail));
    }
    return r.json() as Promise<T>;
  }

  health = () => this.req<{ ok: boolean; model: string }>("/health");
  messages = (limit = 100) => this.req<Message[]>(`/messages?limit=${limit}`);
  journal = (limit = 100) => this.req<JournalEvent[]>(`/journal?limit=${limit}`);
  projects = () => this.req<Project[]>("/projects");
  confirmations = () => this.req<Confirmation[]>("/confirmations");
  resolve = (id: string, approve: boolean) =>
    this.req<{ ok: boolean }>(`/confirmations/${encodeURIComponent(id)}`, {
      method: "POST",
      body: JSON.stringify({ approve }),
    });
  chat = (text: string) =>
    this.req<ChatReply>("/chat", { method: "POST", body: JSON.stringify({ text, channel: "app" }) });
  newSession = () => this.req<{ ok: boolean }>("/session/new", { method: "POST" });
}
