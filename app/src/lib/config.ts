export interface Conn {
  server: string; // https://pc.tailnet.ts.net или http://127.0.0.1:8000
  token: string;
}

const KEY = "jarvis.conn";

/** Если приложение открыто с самого ядра (/app/), сервер — тот же адрес. */
export function defaultServer(loc?: { origin: string; pathname: string }): string {
  loc ??= typeof location === "undefined" ? undefined : location;
  return loc?.pathname.startsWith("/app") ? loc.origin : "http://127.0.0.1:8000";
}

export function loadConn(): Conn {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) {
      const c = JSON.parse(raw);
      if (typeof c.server === "string" && typeof c.token === "string") return c;
    }
  } catch {
    /* хранилище недоступно — работаем с настройками по умолчанию */
  }
  return { server: defaultServer(), token: "" };
}

export function saveConn(c: Conn): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(c));
  } catch {
    /* не критично */
  }
}

export function httpUrl(c: Conn, path: string): string {
  return c.server.replace(/\/+$/, "") + path;
}

/** Браузеры не умеют заголовки у WebSocket, поэтому токен — в query. */
export function wsUrl(c: Conn, path: string): string {
  const u = new URL(path, c.server.replace(/\/+$/, "") + "/");
  u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
  u.searchParams.set("token", c.token);
  return u.toString();
}
