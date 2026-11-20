<script setup lang="ts">
/**
 * Вкладка «Дом». Состояние видно сразу: включённое — в цвете своего смысла, выключенное приглушено.
 * Плитка: нажатие переключает, протяжка меняет уровень, долгое нажатие или «⋯» открывает карточку.
 * Сетка сама занимает ширину экрана: две колонки на телефоне, до шести на большом мониторе.
 */
import { computed, onMounted, reactive, ref, watch } from "vue";
import type { HomeItem } from "../lib/api";
import { desktop } from "../lib/desktop";
import { Gesture, levelAt, LONG_MS } from "../lib/gesture";
import {
  type Before, groupHome, iconOf, isPress, layoutRooms, levelOf, type PillKind, pillOf, pills, reading, shortName,
  snapshot, stateText, tileState, tone,
} from "../lib/home";
import {
  flowAnswer, flowClose, flowOpen, homeAct, homeReload, refreshHome, refreshScenarios, runScenario, state,
} from "../lib/state";
import DeviceSheet from "./DeviceSheet.vue";

onMounted(() => {
  void refreshHome();
  void refreshScenarios();
});

const showSecondary = ref(false);
const adding = ref(false); // блок «Новые устройства» раскрыт
const only = ref<PillKind | "">(""); // нажатый итог: показываем только то, что в него вошло
const room = ref(""); // выбранная комната; пусто — все
const opened = ref(""); // устройство, чья карточка открыта
const totals = computed(() => pills(state.home?.items ?? []));
const rooms = computed(() => {
  const items = state.home?.items ?? [];
  const shown = only.value ? items.filter((i) => pillOf(i) === only.value) : items;
  return layoutRooms(groupHome(shown, showSecondary.value));
});
const visible = computed(() => (room.value ? rooms.value.filter((r) => r.name === room.value) : rooms.value));
const sheet = computed(() => state.home?.items.find((i) => i.entity_id === opened.value) ?? null);
const hidden = computed(() => (state.home?.items ?? []).filter((i) => i.secondary).length);
const quick = computed(() => state.scenarios.filter((s) => s.enabled && !s.error).slice(0, 5));
watch(totals, (list) => {
  if (only.value && !list.some((p) => p.kind === only.value)) only.value = ""; // итог исчез — показываем всё
});
watch(rooms, (list) => {
  if (room.value && !list.some((r) => r.name === room.value)) room.value = "";
});

// --- жесты плитки ---
const gesture = new Gesture();
let held: { item: HomeItem; before: Before; rect: DOMRect; timer: ReturnType<typeof setTimeout> } | null = null;
const dragging = ref("");

function tap(i: HomeItem) {
  if (!i.available) opened.value = i.entity_id;
  else void homeAct(i, isPress(i) ? "on" : i.on ? "off" : "on");
}
function down(i: HomeItem, e: PointerEvent) {
  if (e.button !== 0) return;
  const el = e.currentTarget as HTMLElement;
  gesture.down(e.clientX, e.clientY, e.timeStamp);
  const timer = setTimeout(() => {
    if (!gesture.still) return;
    gesture.cancel(); // долгое нажатие сработало — отпускание уже ничего не переключит
    opened.value = i.entity_id;
  }, LONG_MS);
  held = { item: i, before: snapshot(i), rect: el.getBoundingClientRect(), timer };
}
function move(e: PointerEvent) {
  if (!held) return;
  const level = levelOf(held.item);
  if (!gesture.move(e.clientX, e.clientY, !!level) || !level) return;
  if (!dragging.value) (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
  dragging.value = held.item.entity_id;
  const value = levelAt(e.clientX, held.rect.left, held.rect.width);
  // плитка следует за пальцем; команда уйдёт один раз, когда палец отпустят
  if (level.action === "brightness") Object.assign(held.item, { brightness: value, on: true });
  else if (level.action === "volume") held.item.volume = value;
  else held.item.position = value;
}
function up(e: PointerEvent) {
  if (!held) return;
  const { item, before, timer } = held;
  clearTimeout(timer);
  held = null;
  dragging.value = "";
  const outcome = gesture.up(e.timeStamp);
  const level = levelOf(item);
  if (outcome === "tap") tap(item);
  else if (outcome === "drag" && level) void homeAct(item, level.action, level.value, before);
}
function cancel() {
  if (!held) return;
  clearTimeout(held.timer);
  if (dragging.value) Object.assign(held.item, held.before); // жест сорван — вернуть, как было
  held = null;
  dragging.value = "";
  gesture.cancel();
}
const fill = (i: HomeItem) => {
  const level = levelOf(i);
  return i.on ? `${level ? level.value : 100}%` : level && i.domain !== "light" ? `${level.value}%` : "0%";
};
const lightsOff = (list: HomeItem[]) => list.forEach((i) => void homeAct(i, "off"));

// --- мастер добавления: ответы на текущий шаг сбрасываются, когда приходит следующий ---
const answers = reactive<Record<string, unknown>>({});
watch(
  () => state.flow,
  (step) => {
    for (const key of Object.keys(answers)) delete answers[key];
    for (const f of step?.fields ?? []) answers[f.name] = f.default ?? (f.kind === "boolean" ? false : "");
  },
);
function submit() {
  const data: Record<string, unknown> = {};
  for (const f of state.flow?.fields ?? []) {
    const v = answers[f.name];
    if (v === "" || v === null || v === undefined) continue; // пустое необязательное поле не отправляем
    data[f.name] = f.kind === "number" ? Number(v) : v;
  }
  void flowAnswer(data);
}
</script>

<template>
  <div class="page scroll home">
    <div class="head">
      <h3>Дом <span v-if="!state.homeFresh && state.home" class="badge">обновляю…</span></h3>
      <div class="actions">
        <button class="small" :aria-expanded="adding" @click="adding = !adding">＋ Устройство</button>
        <button class="small" aria-label="Обновить" @click="refreshHome">⟳</button>
      </div>
    </div>
    <div v-if="state.homeError" class="card err small" role="alert" @click="state.homeError = ''">{{ state.homeError }}</div>
    <div v-if="!state.home && !state.homeError" class="empty">Загружаю устройства…</div>

    <template v-if="state.home">
      <div v-if="totals.length" class="strip" role="toolbar" aria-label="Итоги по дому">
        <button
          v-for="p in totals"
          :key="p.kind"
          class="pill" :class="[p.kind, { active: only === p.kind }]" :aria-pressed="only === p.kind"
          @click="only = only === p.kind ? '' : p.kind"
        >
          {{ p.text }}
        </button>
        <button v-if="only" class="pill" @click="only = ''">Показать всё</button>
      </div>

      <div v-if="quick.length" class="strip" aria-label="Сценарии">
        <button
          v-for="s in quick" :key="s.id" class="pill scene"
          :disabled="state.scenarioRunning === s.id" :title="s.phrases.map((p) => `«${p}»`).join(', ')"
          @click="runScenario(s.id)"
        >
          ✨ {{ s.id }}
        </button>
      </div>

      <div v-if="rooms.length > 1" class="strip rooms" role="tablist" aria-label="Комнаты">
        <button class="pill" :class="{ active: !room }" role="tab" :aria-selected="!room" @click="room = ''">Все</button>
        <button
          v-for="r in rooms" :key="r.name" class="pill" :class="{ active: room === r.name }"
          role="tab" :aria-selected="room === r.name" @click="room = r.name"
        >
          {{ r.name || "Без комнаты" }}
        </button>
      </div>

      <div v-if="state.flow" class="card flow">
        <b>{{ state.flowTitle || state.flow.title }}</b>
        <div v-if="state.flow.title && state.flow.title !== state.flowTitle" class="small muted">{{ state.flow.title }}</div>
        <div v-if="state.flow.text" class="small pre">{{ state.flow.text }}</div>
        <div v-for="e in state.flow.errors" :key="e" class="small err">{{ e }}</div>
        <form v-if="state.flow.type === 'form'" class="fields" @submit.prevent="submit">
          <label v-for="f in state.flow.fields" :key="f.name" class="small">
            <template v-if="f.kind === 'boolean'">
              <span class="check"><input v-model="answers[f.name]" type="checkbox" /> {{ f.label }}</span>
            </template>
            <template v-else>
              <span class="muted">{{ f.label }}{{ f.required ? " *" : "" }}</span>
              <select v-if="f.kind === 'select'" v-model="answers[f.name]" :required="f.required">
                <option v-for="o in f.options" :key="String(o.value)" :value="o.value">{{ o.label }}</option>
              </select>
              <input v-else v-model="answers[f.name]" :type="f.kind" :required="f.required" autocomplete="off" />
            </template>
          </label>
          <div class="actions">
            <button class="primary small">{{ state.flow.fields.length ? "Далее" : "Добавить" }}</button>
            <button type="button" class="small" @click="flowClose">Отмена</button>
          </div>
        </form>
        <div v-else-if="state.flow.type === 'menu'" class="actions">
          <button v-for="c in state.flow.choices" :key="c.value" class="small" @click="flowAnswer({ next_step_id: c.value })">
            {{ c.label }}
          </button>
          <button class="small" @click="flowClose">Отмена</button>
        </div>
        <div v-else class="actions">
          <button v-if="state.flow.url" class="small" @click="desktop.open(state.flow.url)">Открыть страницу</button>
          <button class="primary small" @click="flowClose">{{ state.flow.type === "create_entry" ? "Готово" : "Закрыть" }}</button>
        </div>
      </div>

      <div v-if="adding" class="card">
        <b>Новые устройства</b>
        <div v-if="!state.home.discovered.length" class="small muted">В сети ничего нового не найдено.</div>
        <div v-for="d in state.home.discovered" :key="d.flow_id" class="line">
          <div class="what">{{ d.title }} <span class="badge">{{ d.handler }}</span></div>
          <button class="small primary" :disabled="!!state.flow" @click="flowOpen(d.flow_id, d.title)">Добавить</button>
        </div>
        <div class="small muted">Устройство, добавленное в приложении производителя, появится после «Найти новые»:</div>
        <div v-for="s in state.home.integrations" :key="s.entry_id" class="line">
          <div class="what">{{ s.domain }} <span class="small muted">· {{ s.title }} · {{ s.devices }}</span></div>
          <button class="small" :disabled="state.homeBusy === s.entry_id" @click="homeReload(s.entry_id)">
            {{ state.homeBusy === s.entry_id ? "Ищу…" : "Найти новые" }}
          </button>
        </div>
        <div v-if="state.home.url" class="actions">
          <button class="small" @click="desktop.open(state.home.url + '/config/integrations/dashboard')">
            Другой производитель — в Home Assistant
          </button>
        </div>
        <label v-if="hidden" class="check small">
          <input v-model="showSecondary" type="checkbox" />
          <span>Показывать настройки устройств ({{ hidden }})</span>
        </label>
      </div>

      <div v-if="!visible.length" class="empty">Устройств нет.</div>
      <div class="board">
      <section v-for="r in visible" :key="r.name" class="room">
        <header>
          <h4>{{ r.name || "Без комнаты" }}</h4>
          <span v-for="s in r.sensors" :key="s.entity_id" class="reading" :title="s.name">
            {{ iconOf(s) }} {{ reading(s) }}
          </span>
          <button v-if="r.lightsOn.length > 1" class="small quiet" @click="lightsOff(r.lightsOn)">Выключить свет</button>
        </header>

        <div class="grid">
          <div
            v-for="i in r.tiles" :key="i.entity_id"
            class="tile" :class="[tone(i), { on: i.on, gone: !i.available, waits: state.homePending[i.entity_id], drag: dragging === i.entity_id }]"
            :style="{ '--fill': fill(i) }"
            role="button" tabindex="0" :aria-pressed="i.on ?? undefined"
            :aria-label="`${i.name}: ${stateText(i) || 'нажать'}`"
            @pointerdown="down(i, $event)" @pointermove="move" @pointerup="up" @pointercancel="cancel"
            @keydown.enter.prevent="tap(i)" @keydown.space.prevent="tap(i)" @contextmenu.prevent="opened = i.entity_id"
          >
            <span class="icon" aria-hidden="true">{{ iconOf(i) }}</span>
            <span class="text">
              <b>{{ i.name }}</b>
              <small>{{ tileState(i) }}</small>
            </span>
            <span v-if="i.exposed" class="voice" title="Управляется голосом" aria-label="Управляется голосом">🎙</span>
            <button
              class="more" aria-label="Подробности и настройки"
              @pointerdown.stop @keydown.stop @click.stop="opened = i.entity_id"
            >
              ⋯
            </button>
          </div>
        </div>

        <details v-for="g in r.folded" :key="g.device" class="fold">
          <summary>{{ g.device }} <span class="badge">{{ g.items.length }}</span></summary>
          <div class="grid">
            <div
              v-for="i in g.items" :key="i.entity_id"
              class="tile" :class="[tone(i), { on: i.on, gone: !i.available, waits: state.homePending[i.entity_id] }]"
              :style="{ '--fill': fill(i) }"
              role="button" tabindex="0" :aria-pressed="i.on ?? undefined" :aria-label="`${i.name}: ${stateText(i) || 'нажать'}`"
              @pointerdown="down(i, $event)" @pointermove="move" @pointerup="up" @pointercancel="cancel"
              @keydown.enter.prevent="tap(i)" @keydown.space.prevent="tap(i)" @contextmenu.prevent="opened = i.entity_id"
            >
              <span class="icon" aria-hidden="true">{{ iconOf(i) }}</span>
              <span class="text">
                <b>{{ shortName(i, true) }}</b>
                <small>{{ tileState(i) }}</small>
              </span>
              <button class="more" aria-label="Подробности и настройки" @pointerdown.stop @keydown.stop @click.stop="opened = i.entity_id">⋯</button>
            </div>
          </div>
        </details>
      </section>
      </div>
    </template>

    <DeviceSheet v-if="sheet" :item="sheet" @close="opened = ''" />
  </div>
</template>

<style scoped>
.home { max-width: 1400px; width: 100%; margin: 0 auto; }
.head { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
h3, h4 { margin: 0; }
.card { display: flex; flex-direction: column; gap: 8px; }
.err { color: var(--danger); cursor: pointer; }
.pre { white-space: pre-wrap; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; }
.check { display: flex; align-items: center; gap: 8px; min-height: 44px; cursor: pointer; }
.check input { width: auto; }
.line { display: flex; align-items: center; gap: 8px; padding-top: 6px; border-top: 1px solid var(--line); }
.what { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.flow { border-color: var(--accent); }
.fields { display: flex; flex-direction: column; gap: 8px; }
.fields label { display: flex; flex-direction: column; gap: 3px; }
select { font: inherit; color: inherit; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 8px 10px; }

/* полосы итогов, сценариев и комнат: в одну строку, на узком экране прокручиваются пальцем */
.strip { display: flex; gap: 8px; overflow-x: auto; scrollbar-width: none; padding-bottom: 2px; flex: none; }
.strip::-webkit-scrollbar { display: none; }
.pill { border-radius: 999px; padding: 0 14px; min-height: 40px; font-size: 14px; white-space: nowrap; flex: none; }
.pill.active { border-color: var(--accent); background: var(--surface-2); }
.pill.light { color: var(--light); }
.pill.power { color: var(--ok); }
.pill.media, .pill.scene { color: var(--ai); }
.pill.climate { color: var(--cool); }
.pill.offline { color: var(--muted); }

/* широкий экран: комнаты встают рядом колонками, а не растягиваются полосами с пустотой справа */
.board { display: grid; grid-template-columns: 1fr; gap: 18px; align-items: start; }
@media (min-width: 900px) { .board { grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 20px 28px; } }
.room { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.room > header { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 12px; margin-top: 6px; }
.room h4 { font-size: 16px; margin-right: auto; }
.reading { font-size: 13px; color: var(--muted); }
.quiet { min-height: 36px; }

/* сетка сама выбирает число колонок: 2 на телефоне, 3–4 на планшете, до 6 на мониторе */
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(156px, 1fr)); gap: 10px; }
@media (max-width: 359px) { .grid { grid-template-columns: 1fr; } }
@media (min-width: 1200px) { .grid { grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 12px; } }

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
.tile .icon { font-size: 24px; flex: none; transition: filter var(--motion), opacity var(--motion); }
.text { display: flex; flex-direction: column; min-width: 0; }
.text b { font-weight: 600; font-size: 14px; line-height: 1.25; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
.text small { color: var(--muted); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.voice { position: absolute; right: 12px; bottom: 6px; font-size: 11px; opacity: .7; }
.more {
  position: absolute; top: 0; right: 0; width: 44px; height: 44px; padding: 0;
  border: 0; background: none; color: var(--muted); font-size: 18px; border-radius: 0 var(--radius) 0 var(--radius);
}
.more:hover { color: var(--text); }
.fold > summary { cursor: pointer; min-height: 44px; display: flex; align-items: center; gap: 8px; color: var(--muted); }
.fold[open] > summary { margin-bottom: 8px; }
@media (prefers-reduced-motion: reduce) { .tile, .tile::before { transition: none; } }
</style>
