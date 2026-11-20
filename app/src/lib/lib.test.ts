import { describe, expect, it } from "vitest";
import { defaultServer, wsUrl } from "./config";
import { Desktop } from "./desktop";
import { seconds, shortPath, usd } from "./format";
import { mergeMessages } from "./messages";
import { concat, downsampleToInt16, int16ToFloat32 } from "./pcm";
import { VoiceClient } from "./voice";

describe("pcm", () => {
  it("downsamples 48k → 16k with averaging and clipping", () => {
    const input = new Float32Array([0.3, 0.3, 0.3, -1.5, -1.5, -1.5, 1, 1, 1]);
    expect(Array.from(downsampleToInt16(input, 48000))).toEqual([Math.round(0.3 * 0x7fff), -0x8000, 0x7fff]);
  });
  it("roundtrips int16 → float32", () => {
    const f = int16ToFloat32(new Int16Array([0, 16384, -32768]).buffer);
    expect(Array.from(f)).toEqual([0, 0.5, -1]);
  });
  it("concats chunks", () => {
    expect(Array.from(concat([new Float32Array([1]), new Float32Array([2, 3])]))).toEqual([1, 2, 3]);
  });
});

describe("config", () => {
  it("builds ws urls with token", () => {
    expect(wsUrl({ server: "https://pc.ts.net/", token: "a b" }, "/voice/ws")).toBe("wss://pc.ts.net/voice/ws?token=a+b");
    expect(wsUrl({ server: "http://127.0.0.1:8000", token: "t" }, "/events/ws")).toBe("ws://127.0.0.1:8000/events/ws?token=t");
  });
  it("uses core origin when served from /app", () => {
    expect(defaultServer({ origin: "https://pc.ts.net", pathname: "/app/" })).toBe("https://pc.ts.net");
    expect(defaultServer({ origin: "http://tauri.localhost", pathname: "/" })).toBe("http://127.0.0.1:8000");
  });
});

describe("messages", () => {
  it("merges without duplicates, ordered by id", () => {
    const m = (id: number) => ({ id, ts: id, channel: "app", role: "user" as const, text: String(id) });
    expect(mergeMessages([m(1), m(3)], [m(2), m(3)]).map((x) => x.id)).toEqual([1, 2, 3]);
  });
});

class FakeSock {
  readyState = 1;
  binaryType = "";
  sent: unknown[] = [];
  onopen: ((e: unknown) => void) | null = null;
  onmessage: ((e: { data: unknown }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  onerror: ((e: unknown) => void) | null = null;
  send(d: unknown) {
    this.sent.push(d);
  }
  close() {
    this.onclose?.({ code: 1000 });
  }
  emit(d: unknown) {
    this.onmessage?.({ data: typeof d === "string" || d instanceof ArrayBuffer ? d : JSON.stringify(d) });
  }
}

describe("voice client", () => {
  it("plays only the current turn and cancels stale audio", async () => {
    const sock = new FakeSock();
    const audio: number[] = [];
    const phrases: string[] = [];
    let done = 0;
    const v = new VoiceClient(() => "ws://x", {
      audio: (b) => audio.push(b.byteLength),
      phrase: (t) => phrases.push(t),
      done: () => done++,
    }, () => sock);
    const p = v.connect();
    sock.emit({ type: "hello", tts: true, stt: true, sample_rate: 22050 });
    await p;
    expect(v.sampleRate).toBe(22050);

    await v.sendText("первый");
    sock.emit({ type: "start", turn: 1 });
    sock.emit({ type: "text", turn: 1, text: "Раз." });
    sock.emit({ type: "audio", turn: 1, bytes: 4 });
    sock.emit(new ArrayBuffer(4));
    expect(v.busy).toBe(true);

    await v.sendText("второй"); // перебили: старый ход отменяется
    expect(JSON.parse(sock.sent[1] as string)).toEqual({ type: "cancel" });
    sock.emit({ type: "audio", turn: 1, bytes: 8 });
    sock.emit(new ArrayBuffer(8)); // хвост первого хода — игнор
    sock.emit({ type: "done", turn: 1 });
    sock.emit({ type: "text", turn: 2, text: "Два." });
    sock.emit({ type: "audio", turn: 2, bytes: 6 });
    sock.emit(new ArrayBuffer(6));
    sock.emit({ type: "done", turn: 2 });

    expect(audio).toEqual([4, 6]);
    expect(phrases).toEqual(["Раз.", "Два."]);
    expect(done).toBe(1);
    expect(v.busy).toBe(false);
  });

  it("rejects on bad token", async () => {
    const sock = new FakeSock();
    const v = new VoiceClient(() => "ws://x", {}, () => sock);
    const p = v.connect();
    sock.onclose?.({ code: 4401 });
    await expect(p).rejects.toThrow("Неверный токен");
  });
});

describe("format", () => {
  it("shortens paths", () => {
    expect(shortPath("/tmp/x/vault/10_Projects/demo.md")).toBe("10_Projects/demo.md");
    expect(shortPath("D:\\Docs\\finance\\Бюджет.xlsx")).toBe("…/finance/Бюджет.xlsx");
    expect(shortPath("a/b")).toBe("a/b");
  });
});

describe("desktop shell bridge", () => {
  it("is absent in a browser: calls return empty values instead of failing", async () => {
    const d = new Desktop({});
    expect(d.present).toBe(false);
    expect(await d.localConn()).toBeNull();
    expect(await d.services()).toEqual([]);
    expect(await d.autostart()).toBeNull();
    expect(await d.serviceAction("jarvis-core", "restart")).toEqual([]);
  });
  it("passes commands to the shell and survives commands a platform lacks", async () => {
    const calls: unknown[] = [];
    const invoke = async (cmd: string, args?: Record<string, unknown>) => {
      calls.push([cmd, args]);
      if (cmd === "local_conn") return { server: "http://127.0.0.1:8010", token: "t" };
      if (cmd === "service_action") throw "Unit jarvis-x.service not found";
      throw new Error("unknown command");
    };
    const d = new Desktop({ __TAURI__: { core: { invoke } } });
    expect(d.present).toBe(true);
    expect(await d.localConn()).toEqual({ server: "http://127.0.0.1:8010", token: "t" });
    expect(await d.autostart()).toBeNull(); // на телефоне автозапуска нет — блок просто не показываем
    await d.setAutostart(true);
    expect(calls[calls.length - 1]).toEqual(["plugin:autostart|enable", undefined]);
    await expect(d.serviceAction("jarvis-x", "start")).rejects.toThrow("not found");
  });
});

describe("format", () => {
  it("names small sums in words and prints seconds the Russian way", () => {
    expect([usd(0), usd(0.004), usd(0.0721)]).toEqual(["$0", "меньше цента", "$0.07"]);
    expect([seconds(0.13), seconds(null)]).toEqual(["0,13 с", "—"]);
  });
});
