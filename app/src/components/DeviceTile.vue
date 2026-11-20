<script setup lang="ts">
/**
 * Плитка устройства. Нажатие переключает, протяжка по горизонтали меняет уровень (яркость, громкость,
 * положение) — заливка следует за пальцем, команда уходит, когда палец отпустили. Долгое нажатие,
 * правая кнопка или «⋯» открывают карточку. С клавиатуры: Enter и пробел переключают.
 */
import { ref } from "vue";
import type { HomeItem } from "../lib/api";
import { Gesture, levelAt, LONG_MS } from "../lib/gesture";
import { type Before, iconOf, isPress, levelOf, shortName, snapshot, stateText, tileState, tone } from "../lib/home";
import { homeAct, state } from "../lib/state";

const props = defineProps<{ item: HomeItem; short?: boolean; room?: boolean }>();

const gesture = new Gesture();
let held: { before: Before; rect: DOMRect; timer: ReturnType<typeof setTimeout> } | null = null;
const dragging = ref(false);
const open = () => (state.sheet = props.item.entity_id);

function tap() {
  const i = props.item;
  if (!i.available) open();
  else void homeAct(i, isPress(i) ? "on" : i.on ? "off" : "on");
}
function down(e: PointerEvent) {
  if (e.button !== 0) return;
  gesture.down(e.clientX, e.clientY, e.timeStamp);
  const timer = setTimeout(() => {
    if (!gesture.still) return;
    gesture.cancel(); // долгое нажатие сработало — отпускание уже ничего не переключит
    open();
  }, LONG_MS);
  held = { before: snapshot(props.item), rect: (e.currentTarget as HTMLElement).getBoundingClientRect(), timer };
}
function move(e: PointerEvent) {
  if (!held) return;
  const level = levelOf(props.item);
  if (!gesture.move(e.clientX, e.clientY, !!level) || !level) return;
  if (!dragging.value) (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
  dragging.value = true;
  const value = levelAt(e.clientX, held.rect.left, held.rect.width);
  if (level.action === "brightness") Object.assign(props.item, { brightness: value, on: true });
  else if (level.action === "volume") props.item.volume = value;
  else props.item.position = value;
}
function up(e: PointerEvent) {
  if (!held) return;
  const { before, timer } = held;
  clearTimeout(timer);
  held = null;
  dragging.value = false;
  const outcome = gesture.up(e.timeStamp);
  const level = levelOf(props.item);
  if (outcome === "tap") tap();
  else if (outcome === "drag" && level) void homeAct(props.item, level.action, level.value, before);
}
function cancel() {
  if (!held) return;
  clearTimeout(held.timer);
  if (dragging.value) Object.assign(props.item, held.before); // жест сорван — вернуть, как было
  held = null;
  dragging.value = false;
  gesture.cancel();
}
function fill(): string {
  const level = levelOf(props.item);
  if (props.item.on) return `${level ? level.value : 100}%`;
  return level && props.item.domain !== "light" ? `${level.value}%` : "0%";
}
</script>

<template>
  <div
    class="tile" :class="[tone(item), { on: item.on, gone: !item.available, waits: state.homePending[item.entity_id], drag: dragging }]"
    :style="{ '--fill': fill() }"
    role="button" tabindex="0" :aria-pressed="item.on ?? undefined" :aria-label="`${item.name}: ${stateText(item) || 'нажать'}`"
    @pointerdown="down" @pointermove="move" @pointerup="up" @pointercancel="cancel"
    @keydown.enter.prevent="tap" @keydown.space.prevent="tap" @contextmenu.prevent="open"
  >
    <span class="icon" aria-hidden="true">{{ iconOf(item) }}</span>
    <span class="text">
      <b>{{ short ? shortName(item, true) : item.name }}</b>
      <small>{{ [tileState(item), room ? item.area : ""].filter(Boolean).join(" · ") }}</small>
    </span>
    <span v-if="item.exposed" class="voice" title="Управляется голосом" aria-label="Управляется голосом">🎙</span>
    <button class="more" aria-label="Подробности и настройки" @pointerdown.stop @keydown.stop @click.stop="open">⋯</button>
  </div>
</template>

<style scoped>
.tile {
  --tone: var(--ok);
  position: relative; overflow: hidden; isolation: isolate;
  min-height: 76px; padding: 12px 40px 12px 12px;
  display: flex; align-items: center; gap: 10px;
  background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
  cursor: pointer; user-select: none; -webkit-user-select: none;
  touch-action: pan-y; /* по вертикали — прокрутка страницы, по горизонтали — наш жест */
  transition: background var(--motion), border-color var(--motion), opacity var(--motion), transform var(--motion);
}
.tile.light { --tone: var(--light); }
.tile.ai { --tone: var(--ai); }
.tile.heat { --tone: var(--heat); }
.tile.cool { --tone: var(--cool); }
.tile::before { /* заливка показывает уровень: яркость, громкость, положение */
  content: ""; position: absolute; inset: 0 auto 0 0; width: var(--fill); z-index: -1;
  background: color-mix(in srgb, var(--tone) 24%, transparent);
  transition: width var(--motion);
}
.tile.drag::before { transition: none; }
.tile.on { border-color: color-mix(in srgb, var(--tone) 55%, transparent); }
.tile:not(.on) .text b { color: var(--muted); } /* выключенное приглушено */
.tile:not(.on) .icon { filter: grayscale(1); opacity: .6; }
.tile.gone { opacity: .5; }
.tile.waits { opacity: .8; }
.tile:hover { background: var(--surface-2); }
.tile:active { transform: scale(.98); }
.tile:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.icon { font-size: 24px; flex: none; transition: filter var(--motion), opacity var(--motion); }
.text { display: flex; flex-direction: column; min-width: 0; }
.text b { font-weight: 600; font-size: 14px; line-height: 1.25; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
.text small { color: var(--muted); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.voice { position: absolute; right: 12px; bottom: 6px; font-size: 11px; opacity: .7; }
.more {
  position: absolute; top: 0; right: 0; width: 44px; height: 44px; padding: 0;
  border: 0; background: none; color: var(--muted); font-size: 18px; border-radius: 0 var(--radius) 0 var(--radius);
}
.more:hover { color: var(--text); }
@media (prefers-reduced-motion: reduce) { .tile, .tile::before { transition: none; } }
</style>
