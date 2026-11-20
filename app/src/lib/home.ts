/** Раскладка вкладки «Дом»: комнаты → устройства → строки. Чистые функции, чтобы проверять без экрана. */
import type { HomeItem } from "./api";

export interface HomeGroup {
  device: string;
  items: HomeItem[];
  fold: boolean; // у устройства много строк (аэрогриль с десятком тумблеров) — свёрнуто, пока не раскроют
}
export interface HomeRoom {
  name: string;
  groups: HomeGroup[];
}

const FOLD_FROM = 4;

/** Комнаты в порядке ядра (без комнаты — в конце), в комнате строки одного устройства идут вместе. */
export function groupHome(items: HomeItem[], withSecondary: boolean): HomeRoom[] {
  const rooms: HomeRoom[] = [];
  for (const item of items) {
    if (item.secondary && !withSecondary) continue;
    let room = rooms.find((r) => r.name === item.area);
    if (!room) rooms.push((room = { name: item.area, groups: [] }));
    let group = item.device ? room.groups.find((g) => g.device === item.device) : undefined;
    if (!group) room.groups.push((group = { device: item.device, items: [], fold: false }));
    group.items.push(item);
  }
  for (const room of rooms) for (const g of room.groups) g.fold = g.items.length >= FOLD_FROM;
  return rooms;
}

/** Внутри устройства его имя в начале строки — повтор: «Гриль Подогрев» → «Подогрев». */
export function shortName(item: HomeItem, grouped: boolean): string {
  const device = item.device.trim();
  if (grouped && device && item.name.startsWith(device) && item.name.length > device.length + 1) {
    return item.name.slice(device.length).trim();
  }
  return item.name;
}

export function stateText(item: HomeItem): string {
  if (!item.available) return "недоступно";
  if (!item.control) return `${item.state}${item.unit ? " " + item.unit : ""}`;
  if (item.domain === "climate") {
    return [item.state, item.current != null ? `сейчас ${item.current}°` : ""].filter(Boolean).join(" · ");
  }
  if (item.on === null) return "";
  const parts = [item.on ? "включено" : "выключено"];
  if (item.on && item.brightness !== undefined) parts.push(`яркость ${item.brightness}%`);
  if (item.volume !== undefined) parts.push(`громкость ${item.volume}%`);
  return parts.join(" · ");
}

// --- живые состояния и команды с откатом ---------------------------------------------------------

/** Пришло событие об устройстве: заменить его строку, порядок не менять. Незнакомое — признак, что нужен новый снимок. */
export function applyItem(items: HomeItem[], item: HomeItem): boolean {
  const at = items.findIndex((i) => i.entity_id === item.entity_id);
  if (at < 0) return false;
  items[at] = { ...items[at], ...item };
  return true;
}

export type Before = Pick<HomeItem, "on" | "brightness" | "volume" | "position" | "temperature">;

export const snapshot = (i: HomeItem): Before => ({
  on: i.on, brightness: i.brightness, volume: i.volume, position: i.position, temperature: i.temperature,
});

/** Показать результат команды сразу, не дожидаясь дома. Возвращает, каким должно стать «включено». */
export function optimistic(item: HomeItem, action: string, value?: number): boolean | null {
  if (action === "on" || action === "off" || action === "toggle") {
    if (item.on !== null) item.on = action === "toggle" ? !item.on : action === "on";
  } else if (action === "brightness" && value !== undefined) {
    item.brightness = value;
    item.on = true;
  } else if (action === "volume" && value !== undefined) item.volume = value;
  else if (action === "position" && value !== undefined) item.position = value;
  else if (action === "temperature" && value !== undefined) item.temperature = value;
  return action === "on" || action === "off" || action === "toggle" || action === "brightness" ? item.on : null;
}

/** Команда не дошла — вернуть строку к тому, что было. */
export function rollback(item: HomeItem, before: Before): void {
  Object.assign(item, before);
}

// --- строка итогов -------------------------------------------------------------------------------

export type PillKind = "light" | "power" | "media" | "climate" | "offline";
export interface Pill {
  kind: PillKind;
  text: string;
}

const POWER = ["switch", "fan", "input_boolean", "humidifier"];

/** К какому итогу относится строка: по нему же список фильтруется нажатием на итог. */
export function pillOf(i: HomeItem): PillKind | null {
  if (i.secondary) return null;
  if (!i.available) return i.control && !["button", "scene", "script"].includes(i.domain) ? "offline" : null;
  if (i.domain === "light" && i.on) return "light";
  if (POWER.includes(i.domain) && i.on) return "power";
  if (i.domain === "media_player" && i.on) return "media";
  if (i.domain === "sensor" && i.kind === "temperature") return "climate";
  return null;
}

/** Итоги сверху вкладки: сколько света и розеток включено, что играет, температура, что не на связи. */
export function pills(items: HomeItem[]): Pill[] {
  const count = (kind: PillKind) => items.filter((i) => pillOf(i) === kind).length;
  const out: Pill[] = [];
  if (count("light")) out.push({ kind: "light", text: `💡 ${count("light")} вкл` });
  if (count("power")) out.push({ kind: "power", text: `🔌 ${count("power")} вкл` });
  if (count("media")) out.push({ kind: "media", text: `🔊 ${count("media")}` });
  const temps = items.filter((i) => pillOf(i) === "climate").map((i) => Number(i.state)).filter((n) => !Number.isNaN(n));
  if (temps.length) {
    const mean = temps.reduce((a, b) => a + b, 0) / temps.length;
    out.push({ kind: "climate", text: `🌡 ${mean.toFixed(1).replace(".", ",")}°` });
  }
  if (count("offline")) out.push({ kind: "offline", text: `⚠ ${count("offline")} не на связи` });
  return out;
}

/** Цвет включённого устройства — по его смыслу, а не по типу плитки. */
export function tone(i: HomeItem): "light" | "ok" | "ai" | "heat" | "cool" {
  if (i.domain === "light") return "light";
  if (i.domain === "media_player") return "ai";
  if (i.domain === "climate") return "heat";
  if (i.domain === "cover") return "cool";
  return "ok";
}

/** Кэш снимка для мгновенного старта: только комнаты и строки, без учётных записей служб. */
export function cacheable(home: { areas: string[]; items: HomeItem[] }): string {
  return JSON.stringify({ areas: home.areas, items: home.items });
}

export function fromCache(raw: string | null): { url: string; areas: string[]; items: HomeItem[]; integrations: []; discovered: [] } | null {
  try {
    const data = JSON.parse(raw ?? "");
    if (!Array.isArray(data?.items) || !Array.isArray(data?.areas)) return null;
    return { url: "", areas: data.areas, items: data.items, integrations: [], discovered: [] };
  } catch {
    return null;
  }
}

// --- плитки ----------------------------------------------------------------------------------------

export const isPress = (i: HomeItem): boolean => ["button", "scene", "script"].includes(i.domain);

/** Значок по смыслу устройства; сырой идентификатор в интерфейс не выводится. */
export function iconOf(i: HomeItem): string {
  const byDomain: Record<string, string> = {
    light: "💡", fan: "🌀", cover: "🪟", climate: "🌡", vacuum: "🧹", humidifier: "💧", scene: "✨", script: "▶️", button: "🔘",
  };
  if (i.domain === "media_player") return i.kind === "tv" || /(^|\s)(tv|тв|телевизор)/i.test(i.name) ? "📺" : "🔊";
  if (i.domain === "switch" || i.domain === "input_boolean") return i.kind === "outlet" ? "🔌" : "⏻";
  if (i.domain === "sensor" || i.domain === "binary_sensor") {
    const byKind: Record<string, string> = {
      temperature: "🌡", humidity: "💧", battery: "🔋", power: "⚡", energy: "⚡", door: "🚪", window: "🪟",
      motion: "🏃", occupancy: "🧍", moisture: "💦", smoke: "🔥", gas: "⛽", illuminance: "☀️",
    };
    return byKind[i.kind] ?? "📟";
  }
  return byDomain[i.domain] ?? "⚙️";
}

/** Уровень, который меняется протяжкой по плитке: яркость лампы, громкость колонки, положение штор. */
export function levelOf(i: HomeItem): { action: "brightness" | "volume" | "position"; value: number } | null {
  if (!i.available) return null;
  if (i.domain === "light" && i.dimmable) return { action: "brightness", value: i.on ? i.brightness ?? 100 : 0 };
  if (i.volume !== undefined) return { action: "volume", value: i.volume };
  if (i.position !== undefined) return { action: "position", value: i.position };
  return null;
}

export interface RoomLayout {
  name: string;
  tiles: HomeItem[]; // управляемое — сеткой плиток
  sensors: HomeItem[]; // показания — строкой в заголовке комнаты, плитка им не нужна
  folded: HomeGroup[]; // устройства с множеством строк (пульты, аэрогриль) — свёрнуты
  lightsOn: HomeItem[]; // для «выключить свет в комнате»
}

export function layoutRooms(rooms: HomeRoom[]): RoomLayout[] {
  return rooms.map((room) => {
    const open = room.groups.filter((g) => !g.fold).flatMap((g) => g.items);
    const all = room.groups.flatMap((g) => g.items);
    return {
      name: room.name,
      tiles: open.filter((i) => i.control),
      sensors: all.filter((i) => !i.control && i.available),
      folded: room.groups.filter((g) => g.fold).map((g) => ({ ...g, items: g.items.filter((i) => i.control) })).filter((g) => g.items.length),
      lightsOn: all.filter((i) => i.domain === "light" && i.on === true),
    };
  });
}

/** Фраза-команда для устройства: шаг сценария «включить» или «выключить» с названием и комнатой. */
export function deviceStep(i: HomeItem, turn: "on" | "off"): Record<string, unknown> {
  return { [turn]: i.area ? { name: i.name, area: i.area } : i.name };
}

/** Название сценария из фразы: первая буква заглавная, только то, что допустимо в имени заметки. */
export function commandName(phrase: string): string {
  const text = phrase.replace(/[^\p{L}\p{N} -]/gu, " ").replace(/\s+/g, " ").trim().slice(0, 60);
  return text ? text[0].toUpperCase() + text.slice(1) : "";
}

/** Короткое состояние для плитки: на ней мало места, подробности — в карточке. */
export function tileState(i: HomeItem): string {
  if (!i.available) return "нет связи";
  if (isPress(i)) return "нажать";
  if (i.domain === "climate") return i.current != null ? `${i.current}°` : i.state;
  if (i.on === null) return "";
  const level = levelOf(i);
  if (!i.on) return i.domain === "media_player" && level ? `выкл · ${level.value}%` : "выключено";
  return level ? `${level.value}%` : "включено";
}

/** Показание датчика для человека: не больше двух знаков после запятой, запятая вместо точки. */
export function reading(i: HomeItem): string {
  const n = Number(i.state);
  const value = i.state.trim() !== "" && Number.isFinite(n) ? String(Math.round(n * 100) / 100).replace(".", ",") : i.state;
  return i.unit ? `${value} ${i.unit}` : value;
}
