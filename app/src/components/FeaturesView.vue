<script setup lang="ts">
import { computed, onMounted } from "vue";
import type { Feature } from "../lib/api";
import type { Service } from "../lib/desktop";
import { seconds, STATE_NAMES, usd } from "../lib/format";
import {
  checkSpeech, refreshDesktop, refreshStatus, serviceAction, setAutostart, state, tryPhrase,
} from "../lib/state";

function refresh() {
  void refreshStatus();
  void refreshDesktop();
}
onMounted(refresh);

const busy = computed(() => state.sending || state.voice.phase !== "idle");
const counts = computed(() => {
  const list = state.report?.features ?? [];
  return { ready: list.filter((f) => f.state === "ready").length, all: list.length };
});
const check = computed(() => {
  const c = state.speechCheck;
  if (!c) return "";
  if (c.error) return `❌ ${c.error}`;
  const took = `синтез ${seconds(c.tts_s)}, распознавание ${seconds(c.stt_s)}`;
  return c.ok ? `✅ Речь в порядке: ${took}` : `⚠️ Сказано «${c.said}», услышано «${c.heard}» (${took})`;
});
const order = (f: Feature) => ({ ready: 0, down: 1, setup: 2, off: 3 })[f.state];
const features = computed(() => [...(state.report?.features ?? [])].sort((a, b) => order(a) - order(b)));

function toggle(s: Service) {
  void serviceAction(s.unit, s.enabled || s.active ? "disable" : "enable");
}
</script>

<template>
  <div class="page scroll">
    <div class="head">
      <h3>Функции</h3>
      <button class="small" @click="refresh">Обновить</button>
    </div>

    <div v-if="!state.report" class="empty">Нет сводки от ядра: проверьте подключение на вкладке «Настройки».</div>
    <template v-else>
      <div class="card">
        <b>{{ state.report.name }}: работает {{ counts.ready }} из {{ counts.all }}</b>
        <div class="small muted">
          Агент сегодня: {{ usd(state.report.agent_today.usd) }}, ходов: {{ state.report.agent_today.turns }}.
          Микрофонов на связи: {{ state.report.mics }}.
        </div>
        <div class="small muted legend">
          <span>⚡ отвечает быстрая модель и вслух — доли цента</span>
          <span>🧠 отвечает агент — несколько центов за ход</span>
        </div>
        <div class="actions">
          <button class="small" :disabled="state.checkingSpeech" @click="checkSpeech">
            {{ state.checkingSpeech ? "Проверяю…" : "Проверить речь" }}
          </button>
        </div>
        <div v-if="check" class="small" role="status">{{ check }}</div>
      </div>

      <div v-if="state.services.length || state.autostart !== null" class="card">
        <b>Этот компьютер</b>
        <label v-if="state.autostart !== null" class="row">
          <input type="checkbox" :checked="state.autostart" @change="setAutostart(!state.autostart)" />
          <span>Запускать окно при входе в систему (свёрнутым в трей)</span>
        </label>
        <div v-for="s in state.services" :key="s.unit" class="svc">
          <i class="lamp" :class="{ on: s.active }"></i>
          <div class="what">
            <div>{{ s.title }} <span class="badge">{{ s.active ? "работает" : "остановлена" }}</span></div>
            <div class="small muted">{{ s.note }}</div>
          </div>
          <button v-if="s.optional" class="small" :disabled="state.serviceBusy === s.unit" @click="toggle(s)">
            {{ s.enabled || s.active ? "Выключить" : "Включить" }}
          </button>
          <button
            v-else
            class="small"
            :disabled="state.serviceBusy === s.unit"
            @click="serviceAction(s.unit, s.active ? 'restart' : 'start')"
          >
            {{ s.active ? "Перезапустить" : "Запустить" }}
          </button>
        </div>
      </div>

      <div v-for="f in features" :key="f.id" class="card" :class="f.state">
        <div class="title">
          <b>{{ f.title }}</b>
          <span class="badge" :class="f.state">{{ STATE_NAMES[f.state] }}</span>
        </div>
        <div v-if="f.note" class="small muted">{{ f.note }}</div>
        <div v-if="f.hint" class="small hint">Чтобы включить: {{ f.hint }}</div>
        <div v-for="t in f.tries" :key="t.text" class="try">
          <button class="small" :disabled="busy" :title="t.via === 'fast' ? 'Быстрая модель' : 'Агент'" @click="tryPhrase(t)">
            {{ t.via === "fast" ? "⚡" : "🧠" }} {{ t.text }}
          </button>
          <span v-if="t.effect" class="small muted">{{ t.effect }}</span>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.head { display: flex; justify-content: space-between; align-items: center; }
h3 { margin: 0; }
.card { display: flex; flex-direction: column; gap: 6px; }
.card.off, .card.setup { opacity: .8; }
.title { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.badge.ready { color: var(--ok); }
.badge.down { color: var(--danger); }
.badge.setup { color: var(--warn); }
.legend { display: flex; flex-direction: column; }
.actions { display: flex; gap: 8px; }
.hint { color: var(--warn); }
.try { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.try button { text-align: left; }
.row { display: flex; align-items: center; gap: 8px; cursor: pointer; }
.row input { width: auto; }
.svc { display: flex; align-items: center; gap: 10px; padding-top: 6px; border-top: 1px solid var(--line); }
.svc .what { flex: 1; min-width: 0; }
.lamp { width: 9px; height: 9px; border-radius: 50%; background: var(--muted); flex: none; }
.lamp.on { background: var(--ok); }
</style>
