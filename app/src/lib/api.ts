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
/** Таймер, напоминание или будильник из планировщика ядра. */
export interface Scheduled {
  id: number;
  kind: "timer" | "reminder" | "alarm";
  text: string;
  title: string;
  due: number;
  repeat: string;
  state: "pending" | "ringing";
}
/** Сценарий из vault: фраза или время запускают цепочку шагов. */
export interface Scenario {
  id: string;
  phrases: string[];
  schedule: string;
  enabled: boolean;
  without_name: boolean;
  steps: string[];
  error: string;
  file: string;
}
/** Фраза для пробы: fast отвечает быстрая модель (как голосу), agent — агент. */
export interface Try {
  text: string;
  via: "fast" | "agent";
  effect: string;
}
export interface Feature {
  id: string;
  title: string;
  state: "ready" | "setup" | "off" | "down";
  note: string;
  hint: string;
  tries: Try[];
}
export interface StatusReport {
  name: string;
  wake: string[];
  mics: number;
  spent_today: { usd: number; turns: number }; // агент и быстрая модель вместе
  features: Feature[];
}
export interface SpeechCheck {
  ok: boolean;
  said: string;
  heard: string;
  tts_s: number | null;
  stt_s: number | null;
  error: string;
}
/** Строка вкладки «Дом»: устройство или датчик из Home Assistant. */
export interface HomeItem {
  entity_id: string;
  domain: string;
  kind: string;
  name: string;
  device: string;
  area: string;
  control: boolean;
  available: boolean;
  exposed: boolean; // открыто голосовому ассистенту
  secondary: boolean; // настройка или диагностика устройства
  state: string;
  on: boolean | null;
  unit: string;
  brightness?: number;
  dimmable?: boolean;
  volume?: number;
  temperature?: number | null;
  current?: number | null;
  position?: number;
  area_id?: string;
  aliases?: string[]; // как ещё называть устройство голосом
  renamed?: boolean;
  speaks?: boolean; // колонка умеет произнести текст
}
export interface HomeOverview {
  url: string;
  areas: string[];
  rooms?: { id: string; name: string }[];
  items: HomeItem[];
  integrations: { entry_id: string; domain: string; title: string; state: string; devices: number }[];
  discovered: { flow_id: string; handler: string; title: string }[];
}
export type HomeAction = "on" | "off" | "toggle" | "brightness" | "volume" | "temperature" | "position" | "play_pause";
export interface FlowField {
  name: string;
  label: string;
  kind: "text" | "password" | "number" | "boolean" | "select";
  required: boolean;
  default: unknown;
  options: { value: unknown; label: string }[];
}
/** Шаг мастера добавления устройства в Home Assistant. */
export interface FlowStep {
  flow_id: string;
  handler: string;
  type: string;
  title: string;
  text: string;
  errors: string[];
  url: string;
  fields: FlowField[];
  choices: { value: string; label: string }[];
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

  me = () => this.req<{ role: "owner" | "simple"; name: string }>("/me");
  homeSay = (id: string, text: string) =>
    this.req<{ ok: boolean }>(`/home/entity/${encodeURIComponent(id)}/say`, { method: "POST", body: JSON.stringify({ text }) });
  help = () => this.req<{ ok: boolean; repeated: boolean }>("/help", { method: "POST" });
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
  schedule = () => this.req<Scheduled[]>("/schedule");
  scheduleStop = (id: number) => this.req<{ ok: boolean }>(`/schedule/${id}/stop`, { method: "POST" });
  scheduleSnooze = (id: number, minutes = 10) =>
    this.req<{ ok: boolean }>(`/schedule/${id}/snooze`, { method: "POST", body: JSON.stringify({ minutes }) });
  scheduleCancel = (id: number) => this.req<{ ok: boolean }>(`/schedule/${id}`, { method: "DELETE" });
  scenarios = () => this.req<Scenario[]>("/scenarios");
  scenarioRun = (id: string) =>
    this.req<{ ok: boolean; text: string }>(`/scenarios/${encodeURIComponent(id)}/run`, { method: "POST" });
  scenarioEnable = (id: string, enabled: boolean) =>
    this.req<Scenario>(`/scenarios/${encodeURIComponent(id)}/enabled`, {
      method: "POST",
      body: JSON.stringify({ enabled }),
    });
  chat = (text: string) =>
    this.req<ChatReply>("/chat", { method: "POST", body: JSON.stringify({ text, channel: "app" }) });
  status = () => this.req<StatusReport>("/status");
  speechCheck = () => this.req<SpeechCheck>("/status/speech", { method: "POST" });
  home = () => this.req<HomeOverview>("/home");
  homeAct = (id: string, action: HomeAction, value?: number) =>
    this.req<{ ok: boolean }>(`/home/entity/${encodeURIComponent(id)}`, {
      method: "POST",
      body: JSON.stringify({ action, value: value ?? null }),
    });
  homeExpose = (id: string, exposed: boolean) =>
    this.req<{ ok: boolean }>(`/home/entity/${encodeURIComponent(id)}/expose`, {
      method: "POST",
      body: JSON.stringify({ exposed }),
    });
  homeSettings = (id: string, patch: { name?: string; area_id?: string; new_area?: string; aliases?: string[] }) =>
    this.req<{ ok: boolean }>(`/home/entity/${encodeURIComponent(id)}/settings`, {
      method: "POST",
      body: JSON.stringify(patch),
    });
  scenarioCreate = (body: { name: string; phrases: string[]; steps: unknown[]; without_name?: boolean }) =>
    this.req<{ ok: boolean; text: string }>("/scenarios", { method: "POST", body: JSON.stringify(body) });
  scenarioPhrases = (id: string, phrases: string[], without_name?: boolean) =>
    this.req<Scenario>(`/scenarios/${encodeURIComponent(id)}/phrases`, {
      method: "POST",
      body: JSON.stringify({ phrases, without_name }),
    });
  homeReload = (entry: string) =>
    this.req<{ ok: boolean }>(`/home/integrations/${encodeURIComponent(entry)}/reload`, { method: "POST" });
  homeFlow = (id: string) => this.req<FlowStep>(`/home/flows/${encodeURIComponent(id)}`);
  homeFlowAnswer = (id: string, data: Record<string, unknown>) =>
    this.req<FlowStep>(`/home/flows/${encodeURIComponent(id)}`, { method: "POST", body: JSON.stringify({ data }) });
  newSession = () => this.req<{ ok: boolean }>("/session/new", { method: "POST" });
}
