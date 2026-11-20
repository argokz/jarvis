<script setup lang="ts">
import { onMounted } from "vue";
import type { Scenario } from "../lib/api";
import { enableScenario, refreshScenarios, runScenario, state } from "../lib/state";

onMounted(refreshScenarios);

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
      <div v-if="triggers(s)" class="small muted">{{ triggers(s) }}</div>
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
</style>
