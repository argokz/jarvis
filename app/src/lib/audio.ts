import { concat, downsampleToInt16, int16ToFloat32 } from "./pcm";

/** Запись с микрофона в PCM16 моно 16 кГц — формат, который ждёт /voice/ws. */
export class Recorder {
  private ctx?: AudioContext;
  private stream?: MediaStream;
  private chunks: Float32Array[] = [];
  private startedAt = 0;

  get recording(): boolean {
    return !!this.ctx;
  }

  async start(): Promise<void> {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error("Микрофон недоступен: нужен HTTPS или приложение (Tauri).");
    }
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    const ctx = new AudioContext();
    this.ctx = ctx;
    this.chunks = [];
    await ctx.audioWorklet.addModule(new URL("pcm-recorder.js", document.baseURI).toString());
    const src = ctx.createMediaStreamSource(this.stream);
    const node = new AudioWorkletNode(ctx, "pcm-recorder");
    node.port.onmessage = (e: MessageEvent<Float32Array>) => this.chunks.push(e.data);
    const mute = ctx.createGain();
    mute.gain.value = 0; // узел должен быть подключён к выходу, иначе браузер его не «тянет»
    src.connect(node).connect(mute).connect(ctx.destination);
    this.startedAt = performance.now();
  }

  /** Останавливает запись; возвращает PCM16 16 кГц и длительность в секундах. */
  async stop(): Promise<{ pcm: Int16Array; seconds: number }> {
    const ctx = this.ctx;
    this.stream?.getTracks().forEach((t) => t.stop());
    this.ctx = undefined;
    if (!ctx) return { pcm: new Int16Array(0), seconds: 0 };
    const rate = ctx.sampleRate;
    await ctx.close();
    const pcm = downsampleToInt16(concat(this.chunks), rate, 16000);
    this.chunks = [];
    return { pcm, seconds: (performance.now() - this.startedAt) / 1000 };
  }
}

/** Проигрывание ответов фразами подряд; stop() обрывает всё сразу (перебивание). */
export class Player {
  private ctx?: AudioContext;
  private next = 0;
  private sources = new Set<AudioBufferSourceNode>();

  /** Вызывать из обработчика нажатия: браузеры разрешают звук только после жеста пользователя. */
  unlock(): void {
    this.ctx ??= new AudioContext();
    void this.ctx.resume();
  }

  get playing(): boolean {
    return this.sources.size > 0;
  }

  play(pcm: ArrayBuffer, rate: number): void {
    this.unlock();
    const ctx = this.ctx!;
    const data = int16ToFloat32(pcm);
    if (!data.length) return;
    const buf = ctx.createBuffer(1, data.length, rate);
    buf.copyToChannel(data, 0);
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    const at = Math.max(ctx.currentTime + 0.03, this.next);
    src.start(at);
    this.next = at + buf.duration;
    this.sources.add(src);
    src.onended = () => this.sources.delete(src);
  }

  stop(): void {
    for (const s of this.sources) {
      try {
        s.stop();
      } catch {
        /* уже остановлен */
      }
    }
    this.sources.clear();
    this.next = 0;
  }
}
