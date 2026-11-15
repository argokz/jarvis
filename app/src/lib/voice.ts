/**
 * Клиент /voice/ws (протокол описан в jarvis/voice.py).
 * Сервер нумерует ходы по порядку запросов, поэтому n-й отправленный запрос = ход n:
 * всё, что пришло с другим номером, — хвост отменённого ответа, его игнорируем.
 */
export interface VoiceHandlers {
  transcript?(text: string): void;
  phrase?(text: string): void;
  error?(text: string): void;
  done?(): void;
  audio?(pcm: ArrayBuffer, sampleRate: number): void;
  closed?(): void;
}

interface Sock {
  readyState: number;
  binaryType: string;
  send(data: string | ArrayBufferLike | ArrayBufferView): void;
  close(): void;
  onopen: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onclose: ((ev: { code: number }) => void) | null;
  onerror: ((ev: unknown) => void) | null;
}

export class VoiceClient {
  sampleRate = 48000;
  tts = false;
  stt = false;
  private ws?: Sock;
  private requests = 0;
  private turn: number | null = null;
  private turnDone = true;
  private audioTurn: number | null = null;
  private ready?: Promise<void>;

  constructor(
    private url: () => string,
    private h: VoiceHandlers = {},
    private factory: (url: string) => Sock = (u) => new WebSocket(u) as unknown as Sock,
  ) {}

  get busy(): boolean {
    return this.turn !== null && !this.turnDone;
  }

  connect(): Promise<void> {
    if (this.ws && this.ws.readyState <= 1 && this.ready) return this.ready;
    this.requests = 0; // новое соединение — сервер начинает нумерацию заново
    this.turn = null;
    this.turnDone = true;
    const ws = this.factory(this.url());
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    this.ready = new Promise((resolve, reject) => {
      ws.onmessage = (e) => {
        if (typeof e.data === "string") {
          const msg = JSON.parse(e.data);
          if (msg.type === "hello") {
            this.sampleRate = msg.sample_rate;
            this.tts = msg.tts;
            this.stt = msg.stt;
            resolve();
            return;
          }
          this.onJson(msg);
        } else {
          this.onBinary(e.data as ArrayBuffer);
        }
      };
      ws.onclose = (e) => {
        if (this.ws === ws) {
          this.ws = undefined;
          this.ready = undefined;
          if (this.busy) this.h.error?.("Соединение прервано");
          this.turnDone = true;
          this.h.closed?.();
        }
        reject(new Error(e.code === 4401 ? "Неверный токен" : "Голосовой канал недоступен"));
      };
      ws.onerror = () => {};
    });
    return this.ready;
  }

  close(): void {
    this.ws?.close();
  }

  async sendAudio(pcm: Int16Array): Promise<void> {
    await this.connect();
    this.cancel();
    this.ws!.send(pcm.buffer.slice(pcm.byteOffset, pcm.byteOffset + pcm.byteLength));
    this.waiting();
  }

  async sendText(text: string): Promise<void> {
    await this.connect();
    this.cancel();
    this.ws!.send(JSON.stringify({ type: "text", text }));
    this.waiting();
  }

  /** Перебить текущий ответ. */
  cancel(): void {
    if (this.busy && this.ws?.readyState === 1) this.ws.send(JSON.stringify({ type: "cancel" }));
    this.turn = null;
    this.turnDone = true;
  }

  private waiting(): void {
    this.requests += 1;
    this.turn = this.requests;
    this.turnDone = false;
  }

  private onBinary(data: ArrayBuffer): void {
    if (this.audioTurn !== null && this.audioTurn === this.turn) this.h.audio?.(data, this.sampleRate);
    this.audioTurn = null;
  }

  private onJson(m: { type: string; turn?: number; text?: string }): void {
    if (m.type === "start" || m.type === "pong") return;
    if (m.turn === undefined || m.turn !== this.turn) {
      if (m.type === "audio") this.audioTurn = null;
      return;
    }
    switch (m.type) {
      case "transcript":
        this.h.transcript?.(m.text ?? "");
        break;
      case "text":
        this.h.phrase?.(m.text ?? "");
        break;
      case "audio":
        this.audioTurn = m.turn;
        break;
      case "error":
        this.h.error?.(m.text ?? "Ошибка");
        break;
      case "done":
        this.turnDone = true;
        this.h.done?.();
        break;
    }
  }
}
