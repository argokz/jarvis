<script setup lang="ts">
/**
 * Карточка устройства: управление, голос, название и комната. На телефоне выезжает снизу (большой палец
 * достаёт до всего), на широком экране — окно по центру.
 */
import { computed, reactive, ref, watch } from "vue";
import type { HomeItem } from "../lib/api";
import { commandName, deviceStep, iconOf, isPress, levelOf, stateText } from "../lib/home";
import { createCommand, homeAct, homeExpose, homeSay, homeSettings, state } from "../lib/state";

const props = defineProps<{ item: HomeItem }>();
const emit = defineEmits<{ close: [] }>();

const NEW = "__new__";
const form = reactive({ name: "", room: "", newRoom: "", alias: "", phrase: "", turn: "on" as "on" | "off", say: "" });
const note = ref("");
const busy = ref(false);
const assistant = computed(() => state.report?.name || state.assistant || "Джарвис");
const level = computed(() => levelOf(props.item));
const rooms = computed(() => state.home?.rooms ?? []);
const aliases = computed(() => props.item.aliases ?? []);
const dirty = computed(
  () => form.name.trim() !== props.item.name || (form.room === NEW ? !!form.newRoom.trim() : form.room !== (props.item.area_id ?? "")),
);

watch(
  () => props.item.entity_id,
  () => {
    Object.assign(form, { name: props.item.name, room: props.item.area_id ?? "", newRoom: "", alias: "", phrase: "" });
    note.value = "";
  },
  { immediate: true },
);

async function run(job: () => Promise<string>, done: string) {
  busy.value = true;
  note.value = "";
  try {
    const error = await job();
    note.value = error ? `❌ ${error}` : `✅ ${done}`;
  } finally {
    busy.value = false;
  }
}
const save = () =>
  run(
    () =>
      homeSettings(props.item, {
        ...(form.name.trim() !== props.item.name ? { name: form.name.trim() } : {}),
        ...(form.room === NEW ? { new_area: form.newRoom.trim() } : form.room !== (props.item.area_id ?? "") ? { area_id: form.room } : {}),
      }),
    "Сохранено",
  );
const addAlias = () => {
  const name = form.alias.trim();
  if (!name) return;
  form.alias = "";
  void run(() => homeSettings(props.item, { aliases: [...aliases.value, name] }), `Теперь можно сказать «${name}»`);
};
const dropAlias = (name: string) =>
  run(() => homeSettings(props.item, { aliases: aliases.value.filter((a) => a !== name) }), "Имя убрано");
const addPhrase = () => {
  const phrase = form.phrase.trim();
  if (!phrase) return;
  void run(async () => {
    const error = await createCommand(commandName(phrase), [phrase], [deviceStep(props.item, form.turn)]);
    if (!error) form.phrase = "";
    return error;
  }, `Команда сохранена: «${assistant.value}, ${phrase}»`);
};
const say = () => {
  const text = form.say.trim();
  if (!text) return;
  void run(async () => {
    const error = await homeSay(props.item, text);
    if (!error) form.say = "";
    return error;
  }, "Сказано");
};
function slide(e: Event) {
  if (level.value) void homeAct(props.item, level.value.action, Number((e.target as HTMLInputElement).value));
}
function warm(e: Event) {
  const v = Number((e.target as HTMLInputElement).value);
  if (v) void homeAct(props.item, "temperature", v);
}
</script>

<template>
  <div class="veil" @click.self="emit('close')" @keydown.esc="emit('close')">
    <section class="sheet scroll" role="dialog" aria-modal="true" :aria-label="item.name">
      <header>
        <span class="icon" aria-hidden="true">{{ iconOf(item) }}</span>
        <div class="title">
          <b>{{ item.name }}</b>
          <span class="small muted">{{ [item.area || "Без комнаты", stateText(item)].filter(Boolean).join(" · ") }}</span>
        </div>
        <button class="close" aria-label="Закрыть" @click="emit('close')">✕</button>
      </header>

      <div v-if="item.control && item.available" class="block">
        <div class="row">
          <button v-if="isPress(item)" class="primary big" @click="homeAct(item, 'on')">Нажать</button>
          <button v-else class="big" :class="{ primary: item.on }" @click="homeAct(item, item.on ? 'off' : 'on')">
            {{ item.on ? "Выключить" : "Включить" }}
          </button>
          <button v-if="item.domain === 'media_player'" class="big" aria-label="Пауза или продолжить" @click="homeAct(item, 'play_pause')">⏯</button>
        </div>
        <label v-if="level" class="field">
          <span class="small muted">{{ { brightness: "Яркость", volume: "Громкость", position: "Положение" }[level.action] }}: {{ level.value }}%</span>
          <input type="range" :min="level.action === 'brightness' ? 1 : 0" max="100" :value="level.value" @change="slide" />
        </label>
        <label v-if="item.domain === 'climate'" class="field">
          <span class="small muted">Температура, °C</span>
          <input type="number" min="5" max="35" step="0.5" :value="item.temperature ?? ''" @change="warm" />
        </label>
      </div>
      <div v-else-if="item.control" class="block small muted">Устройство не на связи.</div>

      <form v-if="item.speaks && item.available" class="block" @submit.prevent="say">
        <b>Сказать на колонке</b>
        <div class="row">
          <input v-model="form.say" placeholder="Например: ужин готов" maxlength="300" aria-label="Что сказать на колонке" />
          <button :disabled="busy || !form.say.trim()">Сказать</button>
        </div>
      </form>

      <div v-if="item.control" class="block">
        <b>Голос</b>
        <label class="check">
          <input type="checkbox" :checked="item.exposed" @change="homeExpose(item, !item.exposed)" />
          <span>Ассистент управляет этим устройством</span>
        </label>
        <template v-if="item.exposed">
          <div class="small muted">Как ещё называть: «{{ assistant }}, включи {{ aliases[0] ?? "…" }}»</div>
          <div class="chips">
            <span v-for="a in aliases" :key="a" class="chip">
              {{ a }} <button :aria-label="`Убрать имя ${a}`" :disabled="busy" @click="dropAlias(a)">✕</button>
            </span>
          </div>
          <form class="row" @submit.prevent="addAlias">
            <input v-model="form.alias" placeholder="Например: торшер" maxlength="60" aria-label="Новое имя для голоса" />
            <button :disabled="busy || !form.alias.trim()">Добавить</button>
          </form>

          <template v-if="!isPress(item) && item.domain !== 'climate'">
            <div class="small muted sep">
              Своя фраза — срабатывает сразу, без модели: «{{ assistant }}, {{ form.phrase.trim() || "уютный свет" }}»
            </div>
            <form class="row" @submit.prevent="addPhrase">
              <input v-model="form.phrase" placeholder="Фраза" maxlength="60" aria-label="Фраза команды" />
              <select v-model="form.turn" aria-label="Что сделать">
                <option value="on">включить</option>
                <option value="off">выключить</option>
              </select>
              <button :disabled="busy || !form.phrase.trim()">Создать</button>
            </form>
          </template>
        </template>
        <div v-else class="small muted">Пока галочка снята, голосом и сценариями этим устройством управлять нельзя.</div>
      </div>

      <form class="block" @submit.prevent="save">
        <b>Название и комната</b>
        <label class="field">
          <span class="small muted">Название</span>
          <input v-model="form.name" maxlength="80" required />
        </label>
        <label class="field">
          <span class="small muted">Комната</span>
          <select v-model="form.room">
            <option value="">Без комнаты</option>
            <option v-for="r in rooms" :key="r.id" :value="r.id">{{ r.name }}</option>
            <option :value="NEW">Новая комната…</option>
          </select>
        </label>
        <input v-if="form.room === NEW" v-model="form.newRoom" placeholder="Название комнаты" maxlength="40" aria-label="Название новой комнаты" />
        <button class="primary" :disabled="busy || !dirty">Сохранить</button>
      </form>

      <div v-if="note" class="small note" role="status">{{ note }}</div>
    </section>
  </div>
</template>

<style scoped>
.veil { position: fixed; inset: 0; z-index: 20; background: rgba(0, 0, 0, .55); display: flex; align-items: flex-end; justify-content: center; }
.sheet {
  width: 100%; max-width: 520px; max-height: 88dvh;
  background: var(--surface); border: 1px solid var(--line); border-radius: 20px 20px 0 0;
  padding: 14px 16px calc(18px + env(safe-area-inset-bottom));
  display: flex; flex-direction: column; gap: 14px;
  animation: rise var(--motion);
}
@keyframes rise { from { transform: translateY(24px); opacity: 0; } }
@media (min-width: 700px) {
  .veil { align-items: center; }
  .sheet { border-radius: 20px; }
}
header { display: flex; align-items: center; gap: 12px; }
header .icon { font-size: 28px; }
.title { flex: 1; min-width: 0; display: flex; flex-direction: column; overflow-wrap: anywhere; }
.close { min-width: 44px; min-height: 44px; border-radius: 50%; padding: 0; }
.block { display: flex; flex-direction: column; gap: 8px; padding-top: 12px; border-top: 1px solid var(--line); }
.row { display: flex; gap: 8px; flex-wrap: wrap; }
.row input { flex: 1; min-width: 140px; }
.big { min-height: 48px; flex: 1; }
.field { display: flex; flex-direction: column; gap: 4px; }
.field input[type="range"] { padding: 0; border: 0; height: 32px; accent-color: var(--accent); }
.check { display: flex; align-items: center; gap: 8px; min-height: 44px; cursor: pointer; }
.check input { width: 20px; height: 20px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { display: inline-flex; align-items: center; gap: 4px; padding: 2px 4px 2px 10px; border-radius: 999px; background: var(--surface-2); font-size: 13px; }
.chip button { border: 0; background: none; padding: 6px 8px; min-width: 32px; min-height: 32px; }
.sep { margin-top: 6px; }
.note { padding: 8px 10px; border-radius: 10px; background: var(--surface-2); }
select { font: inherit; color: inherit; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; min-height: 44px; }
button { min-height: 44px; }
</style>
