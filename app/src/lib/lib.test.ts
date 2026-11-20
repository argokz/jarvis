import { describe, expect, it } from "vitest";
import { defaultServer, forgetConn, saveConn, storedElsewhere, wsUrl } from "./config";
import type { HomeItem } from "./api";
import { Desktop } from "./desktop";
import { Gesture, levelAt, LONG_MS } from "./gesture";
import {
  applyItem, cacheable, commandName, deviceStep, fromCache, groupHome, iconOf, layoutRooms, levelOf, optimistic, pillOf,
  pills, reading, rollback, shortName, snapshot, stateText, tileState, tone,
} from "./home";
import { applyTokens, contrast, cssVars, tokens } from "./tokens";
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

describe("home tab layout", () => {
  const row = (over: Partial<HomeItem>): HomeItem => ({
    entity_id: "switch.x", domain: "switch", kind: "", name: "Свет", device: "", area: "Зал", control: true,
    available: true, exposed: false, secondary: false, state: "on", on: true, unit: "", ...over,
  });
  it("groups rows by room and device, folds big devices, hides device settings until asked", () => {
    const grill = (n: number, secondary = false) =>
      row({ entity_id: `switch.g${n}`, name: `Гриль Режим ${n}`, device: "Гриль", area: "Кухня", secondary });
    const items = [row({}), row({ entity_id: "light.l", name: "Лампа" }), grill(1), grill(2), grill(3), grill(4), grill(5, true)];
    const rooms = groupHome(items, false);
    expect(rooms.map((r) => r.name)).toEqual(["Зал", "Кухня"]);
    expect(rooms[0].groups.map((g) => g.items.length)).toEqual([1, 1]); // без устройства — каждая строка сама по себе
    expect(rooms[1].groups).toHaveLength(1);
    expect([rooms[1].groups[0].items.length, rooms[1].groups[0].fold]).toEqual([4, true]);
    expect(groupHome(items, true)[1].groups[0].items).toHaveLength(5);
  });
  it("drops the device name repeated in a row and describes the state", () => {
    expect(shortName(row({ name: "Гриль Подогрев", device: "Гриль" }), true)).toBe("Подогрев");
    expect(shortName(row({ name: "Гриль Подогрев", device: "Гриль" }), false)).toBe("Гриль Подогрев");
    expect(shortName(row({ name: "Гриль", device: "Гриль" }), true)).toBe("Гриль");
    expect(stateText(row({ on: true, brightness: 40 }))).toBe("включено · яркость 40%");
    expect(stateText(row({ on: false, brightness: 40 }))).toBe("выключено");
    expect(stateText(row({ control: false, on: null, state: "21.5", unit: "°C" }))).toBe("21.5 °C");
    expect(stateText(row({ available: false }))).toBe("недоступно");
    expect(stateText(row({ domain: "button", on: null, state: "" }))).toBe("");
  });
});

describe("live home and commands", () => {
  const row = (over: Partial<HomeItem>): HomeItem => ({
    entity_id: "light.l", domain: "light", kind: "", name: "Лампа", device: "", area: "Зал", control: true,
    available: true, exposed: false, secondary: false, state: "on", on: true, unit: "", ...over,
  });
  it("applies an event to its row and asks for a snapshot when the row is unknown", () => {
    const items = [row({}), row({ entity_id: "switch.s", domain: "switch", name: "Свет" })];
    expect(applyItem(items, row({ on: false, brightness: 10 }))).toBe(true);
    expect([items[0].on, items[0].brightness, items[1].entity_id]).toEqual([false, 10, "switch.s"]);
    expect(applyItem(items, row({ entity_id: "light.new" }))).toBe(false);
  });
  it("shows the command at once and restores the row when it fails", () => {
    const lamp = row({ on: false, brightness: 30 });
    const before = snapshot(lamp);
    expect(optimistic(lamp, "brightness", 80)).toBe(true); // яркость включает лампу
    expect([lamp.on, lamp.brightness]).toEqual([true, 80]);
    rollback(lamp, before);
    expect([lamp.on, lamp.brightness]).toEqual([false, 30]);
    expect(optimistic(lamp, "toggle")).toBe(true);
    expect(optimistic(row({ domain: "media_player", volume: 20 }), "volume", 55)).toBeNull(); // громкость «вкл» не меняет
    const button = row({ domain: "button", on: null });
    expect(optimistic(button, "on")).toBeNull();
    expect(button.on).toBeNull();
  });
  it("sums the home up in pills and filters by them", () => {
    const items = [
      row({}), row({ entity_id: "light.2", on: false }),
      row({ entity_id: "switch.a", domain: "switch" }), row({ entity_id: "switch.b", domain: "switch", secondary: true }),
      row({ entity_id: "media_player.m", domain: "media_player" }),
      row({ entity_id: "sensor.t1", domain: "sensor", kind: "temperature", control: false, on: null, state: "21.5" }),
      row({ entity_id: "sensor.t2", domain: "sensor", kind: "temperature", control: false, on: null, state: "22.5" }),
      row({ entity_id: "switch.gone", domain: "switch", available: false, on: null }),
      row({ entity_id: "button.ir", domain: "button", available: false, on: null }), // пульт без связи — не поломка дома
    ];
    expect(pills(items).map((p) => p.text)).toEqual(["💡 1 вкл", "🔌 1 вкл", "🔊 1", "🌡 22,0°", "⚠ 1 не на связи"]);
    expect(items.filter((i) => pillOf(i) === "light").map((i) => i.entity_id)).toEqual(["light.l"]);
    expect(pills([row({ on: false })])).toEqual([]);
    expect([tone(items[0]), tone(items[2]), tone(items[4])]).toEqual(["light", "ok", "ai"]);
  });
  it("caches rooms and rows only, never vendor accounts", () => {
    const raw = cacheable({ areas: ["Зал"], items: [row({})], integrations: [{ title: "me@example.com" }] } as never);
    expect(raw).not.toContain("example.com");
    expect(fromCache(raw)).toMatchObject({ url: "", areas: ["Зал"], integrations: [], discovered: [] });
    expect([fromCache(null), fromCache("{"), fromCache('{"items":1}')]).toEqual([null, null, null]);
  });
});

describe("design tokens", () => {
  it("keeps text readable: WCAG AA on every surface", () => {
    const c = tokens.color;
    for (const surface of [c.bg, c.surface, c["surface-2"]]) {
      expect(contrast(c.text, surface)).toBeGreaterThanOrEqual(4.5);
      expect(contrast(c.muted, surface)).toBeGreaterThanOrEqual(4.5);
      for (const semantic of [c.light, c.cool, c.heat, c.ok, c.danger, c.ai]) {
        expect(contrast(semantic, surface)).toBeGreaterThanOrEqual(3); // значки и акценты
      }
    }
    expect(contrast(c["accent-text"], c.accent)).toBeGreaterThanOrEqual(4.5);
    expect(contrast(c["mine-text"], c.mine)).toBeGreaterThanOrEqual(4.5);
    expect(contrast(c.bg, c.danger)).toBeGreaterThanOrEqual(4.5); // счётчик на красном
    expect(c.bg.toUpperCase()).not.toBe("#000000");
  });
  it("turns tokens into CSS variables", () => {
    const vars = cssVars();
    expect(vars["--bg"]).toBe("#0F1115");
    expect(vars["--motion"]).toMatch(/ms ease-out$/);
    const set: Record<string, string> = {};
    applyTokens({ style: { setProperty: (k, v) => (set[k] = v) } });
    expect(set).toEqual(vars);
  });
});

describe("where the token lives", () => {
  it("keeps a manual connection to another core, drops a copy of the local one", () => {
    const store = new Map<string, string>();
    (globalThis as any).localStorage = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    };
    const local = { server: "http://127.0.0.1:8010", token: "local" };
    expect(storedElsewhere(local)).toBe(false); // ничего не сохранено
    saveConn({ server: "http://127.0.0.1:8010/", token: "copy" });
    expect(storedElsewhere(local)).toBe(false); // тот же сервер — это копия, её можно забыть
    forgetConn();
    expect(store.size).toBe(0);
    saveConn({ server: "https://pc.ts.net", token: "remote" });
    expect(storedElsewhere(local)).toBe(true);
    delete (globalThis as any).localStorage;
  });
});

describe("tile gestures and layout", () => {
  const row = (over: Partial<HomeItem>): HomeItem => ({
    entity_id: "light.l", domain: "light", kind: "", name: "Лампа", device: "", area: "Зал", control: true,
    available: true, exposed: false, secondary: false, state: "on", on: true, unit: "", ...over,
  });
  it("tells a tap from a drag, a long press and a page scroll", () => {
    const g = new Gesture();
    g.down(100, 100, 0);
    expect(g.move(104, 101, true)).toBe(false); // дрожание пальца — ещё не протяжка
    expect(g.up(120)).toBe("tap");
    g.down(100, 100, 0);
    expect(g.move(130, 102, true)).toBe(true);
    expect(g.up(300)).toBe("drag");
    g.down(100, 100, 0);
    expect(g.up(LONG_MS + 1)).toBe("long");
    g.down(100, 100, 0);
    expect(g.move(102, 140, true)).toBe(false); // вертикаль — прокрутка страницы
    expect(g.move(160, 140, true)).toBe(false);
    expect(g.up(200)).toBe("none");
    g.down(100, 100, 0);
    expect(g.move(125, 100, false)).toBe(false); // у выключателя уровня нет — это всё ещё нажатие
    expect(g.up(100)).toBe("tap");
    g.down(100, 100, 0);
    g.cancel();
    expect([g.still, g.up(50)]).toEqual([false, "none"]);
  });
  it("maps the finger position to a level between 1 and 100", () => {
    expect([levelAt(0, 0, 200), levelAt(100, 0, 200), levelAt(200, 0, 200), levelAt(500, 0, 200), levelAt(50, 100, 200)]).toEqual([1, 50, 100, 100, 1]);
    expect(levelAt(10, 0, 0)).toBe(1);
  });
  it("knows which level a tile drags", () => {
    expect(levelOf(row({ dimmable: true, brightness: 40 }))).toEqual({ action: "brightness", value: 40 });
    expect(levelOf(row({ dimmable: true, on: false, brightness: 40 }))).toEqual({ action: "brightness", value: 0 });
    expect(levelOf(row({ dimmable: false }))).toBeNull();
    expect(levelOf(row({ domain: "media_player", volume: 30 }))).toEqual({ action: "volume", value: 30 });
    expect(levelOf(row({ domain: "cover", position: 70 }))).toEqual({ action: "position", value: 70 });
    expect(levelOf(row({ dimmable: true, available: false }))).toBeNull();
  });
  it("lays a room out: tiles, readings in the header, big devices folded", () => {
    const remote = (n: number) => row({ entity_id: `button.b${n}`, domain: "button", name: `Пульт ${n}`, device: "Пульт", on: null });
    const items = [
      row({}), row({ entity_id: "light.2", on: true }),
      row({ entity_id: "sensor.t", domain: "sensor", kind: "temperature", control: false, on: null, state: "21", unit: "°C" }),
      row({ entity_id: "sensor.gone", domain: "sensor", kind: "humidity", control: false, on: null, available: false }),
      remote(1), remote(2), remote(3), remote(4),
    ];
    const [hall] = layoutRooms(groupHome(items, false));
    expect(hall.tiles.map((i) => i.entity_id)).toEqual(["light.l", "light.2"]);
    expect(hall.sensors.map((i) => i.entity_id)).toEqual(["sensor.t"]); // недоступный датчик в заголовок не идёт
    expect([hall.folded.length, hall.folded[0].items.length, hall.lightsOn.length]).toEqual([1, 4, 2]);
  });
  it("rounds sensor readings for people", () => {
    const sensor = (state: string, unit = "") => row({ domain: "sensor", control: false, on: null, state, unit });
    expect([reading(sensor("0.07300000000000001", "kWh")), reading(sensor("21.5", "°C")), reading(sensor("0.0", "W")),
      reading(sensor("open")), reading(sensor(""))]).toEqual(["0,07 kWh", "21,5 °C", "0 W", "open", ""]);
  });
  it("keeps the tile caption short", () => {
    expect([tileState(row({ dimmable: true, brightness: 40 })), tileState(row({ on: false })), tileState(row({ dimmable: false })),
      tileState(row({ available: false })), tileState(row({ domain: "button", on: null })),
      tileState(row({ domain: "media_player", on: false, volume: 30 }))]).toEqual(["40%", "выключено", "включено", "нет связи", "нажать", "выкл · 30%"]);
  });
  it("picks icons by meaning and builds a voice command for a device", () => {
    expect([iconOf(row({})), iconOf(row({ domain: "switch", kind: "outlet" })), iconOf(row({ domain: "media_player", name: "TV" })),
      iconOf(row({ domain: "sensor", kind: "temperature" })), iconOf(row({ domain: "weird" }))]).toEqual(["💡", "🔌", "📺", "🌡", "⚙️"]);
    expect(deviceStep(row({}), "on")).toEqual({ on: { name: "Лампа", area: "Зал" } });
    expect(deviceStep(row({ area: "" }), "off")).toEqual({ off: "Лампа" });
    expect([commandName("уютный свет!"), commandName("  «кино»  "), commandName("!!!")]).toEqual(["Уютный свет", "Кино", ""]);
  });
});
