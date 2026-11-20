<script setup lang="ts">
/**
 * Вкладка «Дом». Сверху — итоги и сценарии, ниже полоса «Сейчас · Все · комнаты».
 * «Сейчас» показывает только включённое и то, что не на связи; «Все» — дом по комнатам.
 * Сетка сама занимает ширину экрана: две колонки на телефоне, комнаты колонками на мониторе.
 */
import { computed, onMounted, reactive, ref, watch } from "vue";
import type { HomeItem } from "../lib/api";
import { desktop } from "../lib/desktop";
import { groupHome, iconOf, layoutRooms, nowView, type PillKind, pillOf, quiet, reading } from "../lib/home";
import { flowAnswer, flowClose, flowOpen, homeAct, homeReload, refreshHome, refreshScenarios, state } from "../lib/state";
import DeviceTile from "./DeviceTile.vue";
import HomeSummary from "./HomeSummary.vue";
import NowView from "./NowView.vue";

onMounted(() => {
  void refreshHome();
  void refreshScenarios();
});

const NOW = "\u0000now"; // не имя комнаты: комнаты называет владелец
const ALL = "";
const VIEW_KEY = "jarvis.home.view";
function remembered(): string {
  try {
    return localStorage.getItem(VIEW_KEY) === "all" ? ALL : NOW;
  } catch {
    return NOW;
  }
}

const showSecondary = ref(false);
const adding = ref(false); // блок «Новые устройства» раскрыт
const only = ref<PillKind | "">(""); // нажатый итог: показываем только то, что в него вошло
const room = ref(remembered()); // «Сейчас», все комнаты или одна комната
const rooms = computed(() => {
  const items = state.home?.items ?? [];
  const shown = only.value ? quiet(items).filter((i) => pillOf(i) === only.value) : items;
  return layoutRooms(groupHome(shown, showSecondary.value));
});
const visible = computed(() => (room.value === ALL ? rooms.value : rooms.value.filter((r) => r.name === room.value)));
const hidden = computed(() => (state.home?.items ?? []).filter((i) => i.secondary).length);
const busyNow = computed(() => {
  const now = nowView(state.home?.items ?? []);
  return now.active.length + now.offline.length + now.busy.length;
});
function show(view: string) {
  room.value = view;
  if (view === NOW || view === ALL) {
    try {
      localStorage.setItem(VIEW_KEY, view === ALL ? "all" : "now"); // с чего открывать вкладку в следующий раз
    } catch {
      /* не критично */
    }
  }
}
watch(only, (kind) => {
  if (kind && room.value === NOW) room.value = ALL; // итог фильтрует дом по комнатам
});
watch(rooms, (list) => {
  if (room.value !== NOW && room.value !== ALL && !list.some((r) => r.name === room.value)) room.value = ALL;
});
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
      <HomeSummary v-model="only" filterable />

      <div class="strip" role="tablist" aria-label="Что показывать">
        <button class="pill" :class="{ active: room === NOW }" role="tab" :aria-selected="room === NOW" @click="show(NOW)">
          Сейчас <span v-if="busyNow" class="count">{{ busyNow }}</span>
        </button>
        <button class="pill" :class="{ active: room === ALL }" role="tab" :aria-selected="room === ALL" @click="show(ALL)">Все</button>
        <button
          v-for="r in rooms" :key="r.name" class="pill" :class="{ active: room === r.name }"
          role="tab" :aria-selected="room === r.name" @click="show(r.name)"
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

      <NowView v-if="room === NOW" />
      <template v-else>
        <div v-if="!visible.length" class="empty">Устройств нет.</div>
        <div class="board">
          <section v-for="r in visible" :key="r.name" class="room">
            <header>
              <h4>{{ r.name || "Без комнаты" }}</h4>
              <span v-for="s in r.sensors" :key="s.entity_id" class="reading" :title="s.name">{{ iconOf(s) }} {{ reading(s) }}</span>
              <button v-if="r.lightsOn.length > 1" class="small quiet" @click="lightsOff(r.lightsOn)">Выключить свет</button>
            </header>
            <div class="grid">
              <DeviceTile v-for="i in r.tiles" :key="i.entity_id" :item="i" />
            </div>
            <details v-for="g in r.folded" :key="g.device" class="fold">
              <summary>{{ g.device }} <span class="badge">{{ g.items.length }}</span></summary>
              <div class="grid">
                <DeviceTile v-for="i in g.items" :key="i.entity_id" :item="i" short />
              </div>
            </details>
          </section>
        </div>
      </template>
    </template>
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

/* полоса «Сейчас · Все · комнаты»: в одну строку, на узком экране прокручивается пальцем */
.strip { display: flex; gap: 8px; overflow-x: auto; scrollbar-width: none; padding-bottom: 2px; flex: none; }
.strip::-webkit-scrollbar { display: none; }
.pill { border-radius: 999px; padding: 0 14px; min-height: 40px; font-size: 14px; white-space: nowrap; flex: none; }
.pill.active { border-color: var(--accent); background: var(--surface-2); }
.count { margin-left: 4px; color: var(--muted); }

/* широкий экран: комнаты встают рядом колонками, а не растягиваются полосами с пустотой справа */
.board { display: grid; grid-template-columns: 1fr; gap: 18px; align-items: start; }
@media (min-width: 900px) { .board { grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 20px 28px; } }
.room { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.room > header { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 12px; margin-top: 6px; }
.room h4 { font-size: 16px; margin-right: auto; }
.reading { font-size: 13px; color: var(--muted); }
.quiet { min-height: 36px; }

/* сетка сама выбирает число колонок: 2 на телефоне, 3–4 на планшете */
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(156px, 1fr)); gap: 10px; }
@media (max-width: 359px) { .grid { grid-template-columns: 1fr; } }
.fold > summary { cursor: pointer; min-height: 44px; display: flex; align-items: center; gap: 8px; color: var(--muted); }
.fold[open] > summary { margin-bottom: 8px; }
</style>
