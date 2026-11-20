<script setup lang="ts">
import { reactive, ref, onMounted } from "vue";
import type { Scenario } from "../lib/api";
import { enableScenario, refreshScenarios, runScenario, scenarioPhrases, state } from "../lib/state";

onMounted(refreshScenarios);

// правка фраз запуска: через запятую, как их и говорят
const editing = ref("");
const draft = reactive({ text: "", bare: false, error: "" });
function edit(s: Scenario) {
  editing.value = s.id;
  Object.assign(draft, { text: s.phrases.join(", "), bare: s.without_name, error: "" });
}
async function savePhrases(s: Scenario) {
  const phrases = draft.text.split(/[,;\n]/).map((p) => p.trim()).filter(Boolean);
  draft.error = await scenarioPhrases(s.id, phrases, draft.bare);
  if (!draft.error) editing.value = "";
}
const assistant = () => state.report?.name ?? "Джарвис";

const triggers = (s: Scenario): string =>
  [s.phrases.map((p) => `«${p}»`).join(", "), s.schedule].filter(Boolean).join(" · ");
</script>

<template>
  <div class="page scroll">
    <div class="head">
      <h3>Сценарии</h3>
      <button class="small" @click="refreshScenarios">Обновить</button>
    </div>
    <div v-if="!state.scenarios.length" class="empty">
      Сценариев пока нет. Попросите ассистента: «когда я говорю „спокойной ночи“ — выключи свет и поставь
      будильник на семь» — или скопируйте 50_Scenarios/_template.md в vault.
    </div>
    <div v-for="s in state.scenarios" :key="s.id" class="card" :class="{ off: !s.enabled }">
      <div class="title">
        <b>{{ s.id }}</b>
        <span v-if="s.error" class="badge err">ошибка</span>
        <span v-else-if="!s.enabled" class="badge">выключен</span>
        <span v-if="s.without_name && !s.error" class="badge" title="Срабатывает и без обращения по имени">без имени</span>
      </div>
      <div v-if="triggers(s) && editing !== s.id" class="small muted">{{ triggers(s) }}</div>
      <form v-if="editing === s.id" class="phrases" @submit.prevent="savePhrases(s)">
        <label class="small muted" :for="`ph-${s.id}`">Фразы запуска через запятую: «{{ assistant() }}, …»</label>
        <input :id="`ph-${s.id}`" v-model="draft.text" placeholder="я дома, я пришёл" />
        <label class="check small">
          <input v-model="draft.bare" type="checkbox" />
          <span>Срабатывать и без обращения по имени (фразы от двух слов)</span>
        </label>
        <div v-if="draft.error" class="small err">{{ draft.error }}</div>
        <div class="actions">
          <button class="small primary">Сохранить фразы</button>
          <button type="button" class="small" @click="editing = ''">Отмена</button>
        </div>
      </form>
      <div v-if="s.error" class="small err">{{ s.error }}</div>
      <ol v-if="s.steps.length" class="steps small">
        <li v-for="(step, i) in s.steps" :key="i">{{ step }}</li>
      </ol>
      <div v-if="state.scenarioSaid?.id === s.id" class="said small" role="status">{{ state.scenarioSaid.text }}</div>
      <div class="actions">
        <button
          class="small primary"
          :disabled="!!s.error || !s.enabled || state.scenarioRunning === s.id"
          @click="runScenario(s.id)"
        >
          {{ state.scenarioRunning === s.id ? "Выполняю…" : "Запустить" }}
        </button>
        <button v-if="editing !== s.id" class="small" @click="edit(s)">Фразы</button>
        <button class="small" @click="enableScenario(s.id, !s.enabled)">
          {{ s.enabled ? "Выключить" : "Включить" }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
h3 { margin: 6px 0; font-size: 15px; }
.head { display: flex; justify-content: space-between; align-items: center; }
.title { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 4px; }
.card.off .title b, .card.off .steps { color: var(--muted); }
.steps { margin: 8px 0 0; padding-left: 22px; }
.steps li { overflow-wrap: anywhere; }
.said { margin-top: 8px; padding: 6px 10px; border-radius: 8px; background: var(--surface-2); }
.actions { display: flex; gap: 8px; margin-top: 10px; }
.err { color: var(--danger); }
.phrases { display: flex; flex-direction: column; gap: 6px; margin-top: 6px; }
.check { display: flex; align-items: center; gap: 8px; min-height: 40px; cursor: pointer; }
.check input { width: auto; }
.actions { flex-wrap: wrap; }
</style>
