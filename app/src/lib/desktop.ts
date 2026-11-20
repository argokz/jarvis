/**
 * Мост к оболочке Tauri на ПК: значок в трее, автозапуск, службы systemd, настройки подключения с этой машины.
 * В браузере и на телефоне оболочки нет — вызовы возвращают пустое, и экран просто не показывает эти блоки.
 */
import type { Conn } from "./config";

/** Служба ассистента на этом ПК (systemd пользователя). */
export interface Service {
  unit: string;
  title: string;
  note: string;
  optional: boolean; // можно выключить совсем: микрофон, запасной голос, клон
  active: boolean;
  enabled: boolean; // запускается при входе в систему
}
export type ServiceAction = "start" | "stop" | "restart" | "enable" | "disable";

type Invoke = (cmd: string, args?: Record<string, unknown>) => Promise<unknown>;
type Host = {
  __TAURI__?: { core?: { invoke?: Invoke } };
  __TAURI_INTERNALS__?: { metadata?: { currentWindow?: { label?: string } } };
};

const invoker = (host: Host): Invoke | undefined => host.__TAURI__?.core?.invoke;

export class Desktop {
  constructor(private host: Host = globalThis as Host) {}

  get present(): boolean {
    return !!invoker(this.host);
  }

  /** Имя окна оболочки: "main" или "quick"; в браузере — пусто. */
  get windowLabel(): string {
    return this.host.__TAURI_INTERNALS__?.metadata?.currentWindow?.label ?? "";
  }

  private async call<T>(cmd: string, args: Record<string, unknown> | undefined, fallback: T): Promise<T> {
    const invoke = invoker(this.host);
    if (!invoke) return fallback;
    try {
      return (await invoke(cmd, args)) as T;
    } catch {
      return fallback; // команда есть не на всех платформах (на телефоне нет служб и автозапуска)
    }
  }

  /** Адрес ядра и токен из .env этого ПК; null, если оболочка их не нашла. */
  localConn = () => this.call<Conn | null>("local_conn", undefined, null);
  services = () => this.call<Service[]>("services", undefined, []);
  showWindow = () => this.call<null>("show_window", undefined, null);
  /** Показать главное окно (из быстрого окна) и спрятать быстрое. */
  openMain = () => this.call<null>("open_main", undefined, null);
  hideQuick = () => this.call<null>("hide_quick", undefined, null);

  /** Открыть ссылку в браузере системы: из окна Tauri обычная ссылка никуда не ведёт. */
  async open(url: string): Promise<void> {
    if (!this.present) {
      window.open(url, "_blank", "noopener");
      return;
    }
    await this.call<null>("open_external", { url }, null);
  }
  autostart = () => this.call<boolean | null>("plugin:autostart|is_enabled", undefined, null);
  setAutostart = (on: boolean) => this.call<null>(`plugin:autostart|${on ? "enable" : "disable"}`, undefined, null);

  /** Действие со службой; ошибку systemd показываем как есть. */
  async serviceAction(unit: string, action: ServiceAction): Promise<Service[]> {
    const invoke = invoker(this.host);
    if (!invoke) return [];
    try {
      return (await invoke("service_action", { unit, action })) as Service[];
    } catch (e) {
      throw new Error(typeof e === "string" ? e : e instanceof Error ? e.message : String(e));
    }
  }
}

export const desktop = new Desktop();
