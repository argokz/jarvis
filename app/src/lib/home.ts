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
