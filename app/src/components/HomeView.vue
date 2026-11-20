<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import type { HomeItem } from "../lib/api";
import { desktop } from "../lib/desktop";
import { groupHome, shortName, stateText } from "../lib/home";
import {
  flowAnswer, flowClose, flowOpen, homeAct, homeExpose, homeReload, refreshHome, state,
} from "../lib/state";

onMounted(refreshHome);

const showSecondary = ref(false);
const rooms = computed(() => groupHome(state.home?.items ?? [], showSecondary.value));
const name = computed(() => state.report?.name ?? "Джарвис");
const hidden = computed(() => (state.home?.items ?? []).filter((i) => i.secondary).length);

// ответы на текущий шаг мастера: сбрасываются, когда приходит следующий шаг
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

const busy = (i: HomeItem) => state.homeBusy === i.entity_id;
const press = (i: HomeItem) => ["button", "scene", "script"].includes(i.domain);
function slide(i: HomeItem, action: "brightness" | "volume" | "position", e: Event) {
  void homeAct(i, action, Number((e.target as HTMLInputElement).value));
}
function warm(i: HomeItem, e: Event) {
  const v = Number((e.target as HTMLInputElement).value);
  if (v) void homeAct(i, "temperature", v);
}
</script>

<template>
  <div class="page scroll">
    <div class="head">
      <h3>Дом</h3>
      <button class="small" @click="refreshHome">Обновить</button>
    </div>
    <div v-if="state.homeError" class="card err small" role="alert" @click="state.homeError = ''">{{ state.homeError }}</div>
    <div v-if="!state.home && !state.homeError" class="empty">Загружаю устройства…</div>

    <template v-if="state.home">
      <div class="card small muted">
        🎙 у устройства — им можно управлять голосом: «{{ name }}, включи лампочку в зале», «{{ name }}, сделай свет
        на половину». Без 🎙 оно работает только с этой вкладки.
        <label v-if="hidden" class="row">
          <input v-model="showSecondary" type="checkbox" />
          <span>Показывать настройки устройств ({{ hidden }})</span>
        </label>
      </div>

      <div v-if="state.flow" class="card flow">
        <b>{{ state.flowTitle || state.flow.title }}</b>
        <div v-if="state.flow.title && state.flow.title !== state.flowTitle" class="small muted">{{ state.flow.title }}</div>
        <div v-if="state.flow.text" class="small pre">{{ state.flow.text }}</div>
        <div v-for="e in state.flow.errors" :key="e" class="small err">{{ e }}</div>

        <form v-if="state.flow.type === 'form'" class="fields" @submit.prevent="submit">
          <label v-for="f in state.flow.fields" :key="f.name" class="small">
            <template v-if="f.kind === 'boolean'">
              <span class="row"><input v-model="answers[f.name]" type="checkbox" /> {{ f.label }}</span>
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

      <div class="card">
        <b>Новые устройства</b>
        <div v-if="!state.home.discovered.length" class="small muted">В сети ничего нового не найдено.</div>
        <div v-for="d in state.home.discovered" :key="d.flow_id" class="line">
          <div class="what">{{ d.title }} <span class="badge">{{ d.handler }}</span></div>
          <button class="small primary" :disabled="!!state.flow" @click="flowOpen(d.flow_id, d.title)">Добавить</button>
        </div>
        <div class="small muted sep">
          Устройство, добавленное в приложении производителя, появится здесь после «Найти новые»:
        </div>
        <div v-for="s in state.home.integrations" :key="s.entry_id" class="line">
          <div class="what">
            {{ s.domain }} <span class="small muted">· {{ s.title }} · строк на вкладке: {{ s.devices }}</span>
          </div>
          <button class="small" :disabled="state.homeBusy === s.entry_id" @click="homeReload(s.entry_id)">
            {{ state.homeBusy === s.entry_id ? "Ищу…" : "Найти новые" }}
          </button>
        </div>
        <div class="actions sep">
          <button class="small" @click="desktop.open(state.home.url + '/config/integrations/dashboard')">
            Другой производитель — в Home Assistant
          </button>
        </div>
      </div>

      <div v-if="!rooms.length" class="empty">Устройств нет.</div>
      <section v-for="room in rooms" :key="room.name" class="card">
        <b>{{ room.name || "Без комнаты" }}</b>
        <component
          :is="g.fold ? 'details' : 'div'"
          v-for="g in room.groups"
          :key="g.device + g.items[0].entity_id"
          class="group"
        >
          <summary v-if="g.fold">{{ g.device }} <span class="badge">{{ g.items.length }}</span></summary>
          <div v-else-if="g.items.length > 1" class="small muted device">{{ g.device }}</div>
          <div v-for="i in g.items" :key="i.entity_id" class="line" :class="{ off: !i.available }">
            <button
              v-if="i.control"
              class="ear"
              :class="{ on: i.exposed }"
              :title="i.exposed ? 'Ассистент управляет этим голосом — нажмите, чтобы закрыть' : 'Голосом недоступно — нажмите, чтобы открыть ассистенту'"
              :aria-pressed="i.exposed"
              @click="homeExpose(i, !i.exposed)"
            >
              🎙
            </button>
            <div class="what">
              <div>{{ shortName(i, g.items.length > 1) }}</div>
              <div class="small muted">{{ stateText(i) }}</div>
              <input
                v-if="i.dimmable && i.on"
                type="range" min="1" max="100" :value="i.brightness ?? 100" aria-label="Яркость"
                @change="slide(i, 'brightness', $event)"
              />
              <input
                v-if="i.volume !== undefined && i.available"
                type="range" min="0" max="100" :value="i.volume" aria-label="Громкость"
                @change="slide(i, 'volume', $event)"
              />
              <input
                v-if="i.position !== undefined && i.available"
                type="range" min="0" max="100" :value="i.position" aria-label="Положение"
                @change="slide(i, 'position', $event)"
              />
            </div>
            <template v-if="i.control && i.available">
              <input
                v-if="i.domain === 'climate'"
                class="temp" type="number" min="5" max="35" step="0.5" :value="i.temperature ?? ''" aria-label="Температура"
                @change="warm(i, $event)"
              />
              <button v-if="i.domain === 'media_player'" class="small" :disabled="busy(i)" @click="homeAct(i, 'play_pause')">⏯</button>
              <button v-if="press(i)" class="small" :disabled="busy(i)" @click="homeAct(i, 'on')">Нажать</button>
              <button
                v-else
                class="small" :class="{ primary: i.on }" :disabled="busy(i)"
                @click="homeAct(i, i.on ? 'off' : 'on')"
              >
                {{ i.on ? "Выключить" : "Включить" }}
              </button>
            </template>
          </div>
        </component>
      </section>
    </template>
  </div>
</template>

<style scoped>
.head { display: flex; justify-content: space-between; align-items: center; }
h3 { margin: 0; }
.card { display: flex; flex-direction: column; gap: 6px; }
.err { color: var(--danger); cursor: pointer; }
.pre { white-space: pre-wrap; }
.row { display: flex; align-items: center; gap: 8px; margin-top: 6px; cursor: pointer; }
.row input, .fields input[type="checkbox"] { width: auto; }
.line { display: flex; align-items: center; gap: 8px; padding-top: 6px; border-top: 1px solid var(--line); }
.line.off { opacity: .55; }
.what { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.what input[type="range"] { width: 100%; padding: 0; border: 0; margin-top: 4px; accent-color: var(--accent); }
.sep { margin-top: 6px; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; }
.group { display: flex; flex-direction: column; gap: 6px; }
details.group > summary { cursor: pointer; padding-top: 6px; border-top: 1px solid var(--line); }
.device { padding-top: 6px; border-top: 1px solid var(--line); }
.device + .line { border-top: 0; padding-top: 0; }
.ear { padding: 4px 6px; border-radius: 8px; opacity: .3; filter: grayscale(1); flex: none; }
.ear.on { opacity: 1; filter: none; border-color: var(--accent); }
.temp { width: 76px; flex: none; }
.flow { border-color: var(--accent); }
.fields { display: flex; flex-direction: column; gap: 8px; }
.fields label { display: flex; flex-direction: column; gap: 3px; }
select { font: inherit; color: inherit; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 8px 10px; }
</style>
