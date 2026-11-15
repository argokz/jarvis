/** WebSocket с автоматическим переподключением (экспоненциальная пауза до 15 с). */
export type Status = "connecting" | "online" | "offline";

export class Reconnecting {
  private ws?: WebSocket;
  private delay = 1000;
  private timer?: ReturnType<typeof setTimeout>;
  private stopped = true;

  constructor(
    private url: () => string,
    private onMessage: (data: unknown) => void,
    private onStatus: (s: Status) => void,
  ) {}

  start(): void {
    this.stopped = false;
    this.open();
  }

  stop(): void {
    this.stopped = true;
    clearTimeout(this.timer);
    this.ws?.close();
    this.ws = undefined;
    this.onStatus("offline");
  }

  restart(): void {
    this.stop();
    this.delay = 1000;
    this.start();
  }

  private open(): void {
    this.onStatus("connecting");
    const ws = new WebSocket(this.url());
    this.ws = ws;
    ws.onopen = () => {
      this.delay = 1000;
      this.onStatus("online");
    };
    ws.onmessage = (e) => {
      try {
        this.onMessage(JSON.parse(e.data));
      } catch {
        /* не JSON — пропускаем */
      }
    };
    ws.onclose = (e) => {
      if (this.ws !== ws) return;
      this.onStatus("offline");
      if (this.stopped || e.code === 4401) return; // неверный токен — не долбимся
      this.timer = setTimeout(() => this.open(), this.delay);
      this.delay = Math.min(this.delay * 2, 15000);
    };
  }
}
