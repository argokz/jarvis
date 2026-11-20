<script setup lang="ts">
/**
 * Быстрое окно из трея или по горячей клавише: что сейчас включено, сценарии и одна строка вопроса.
 * Сделал дело — Esc, и окно исчезло. Всё остальное — в главном окне.
 */
import { computed, onMounted, ref } from "vue";
import { desktop } from "../lib/desktop";
import { askFast, refreshHome, refreshScenarios, state } from "../lib/state";
import HomeSummary from "./HomeSummary.vue";
import NowView from "./NowView.vue";

const draft = ref("");
const field = ref<HTMLInputElement>();
const assistant = computed(() => state.report?.name ?? "Джарвис");
const answer = computed(() => state.voice.phrases.join(" "));

onMounted(() => {
  void refreshHome();
  void refreshScenarios();
  field.value?.focus();
});
function ask() {
  const text = draft.value.trim();
  if (!text) return;
  draft.value = "";
  void askFast(text);
}
</script>

<template>
  <div class="quick" @keydown.esc="desktop.hideQuick()">
    <header>
      <strong>{{ assistant }}</strong>
      <span class="status" :class="state.status"><i></i></span>
      <button class="small" @click="desktop.openMain()">Открыть всё</button>
      <button class="small" aria-label="Закрыть" @click="desktop.hideQuick()">✕</button>
    </header>

    <form class="ask" @submit.prevent="ask">
      <input ref="field" v-model="draft" :placeholder="`Спросить ${assistant}…`" aria-label="Вопрос ассистенту" />
      <button class="primary" :disabled="!draft.trim() || state.voice.phase === 'thinking'" aria-label="Отправить">➤</button>
    </form>
    <div v-if="state.voice.phase !== 'idle' || answer" class="reply small" role="status">
      <span v-if="state.voice.phase === 'thinking' && !answer" class="muted">Думаю…</span>
      {{ answer }}
    </div>
    <div v-if="state.voice.error" class="small err">{{ state.voice.error }}</div>
    <div v-if="state.homeError" class="small err" @click="state.homeError = ''">{{ state.homeError }}</div>

    <div class="body scroll">
      <div v-if="!state.home" class="empty">{{ state.conn.token ? "Загружаю дом…" : "Нет подключения к ядру." }}</div>
      <template v-else>
        <HomeSummary />
        <NowView />
      </template>
    </div>
  </div>
</template>

<style scoped>
.quick { height: 100%; display: flex; flex-direction: column; gap: 10px; padding: 12px; }
header { display: flex; align-items: center; gap: 8px; }
header strong { margin-right: auto; }
.status i { display: block; width: 8px; height: 8px; border-radius: 50%; background: var(--danger); }
.status.online i { background: var(--ok); }
.status.connecting i { background: var(--warn); }
.ask { display: flex; gap: 8px; }
.ask button { min-width: 44px; }
.reply { padding: 8px 10px; border-radius: 10px; background: var(--surface-2); }
.err { color: var(--danger); }
.body { flex: 1; min-height: 0; display: flex; flex-direction: column; gap: 10px; }
</style>
