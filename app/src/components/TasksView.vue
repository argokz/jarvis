<script setup lang="ts">
import { computed } from "vue";
import { channelName, kindName, shortPath, time, toolName } from "../lib/format";
import { refreshAll, resolveConfirmation, state } from "../lib/state";

const feed = computed(() => state.journal.filter((j) => j.kind !== "user" && j.kind !== "assistant"));
const icon = (k: string) =>
  ({ tool: "🔧", denied: "⛔", error: "❗", confirm_asked: "❓", confirm_resolved: "✔️" })[k] ?? "•";

function summary(j: { kind: string; name: string; text: string }): string {
  try {
    const p = JSON.parse(j.text || "null");
    if (j.kind === "denied") return p?.reason ?? j.text;
    if (j.kind === "confirm_resolved") return `${p?.approved ? "подтверждено" : "отклонено"} — ${j.name}`;
    if (j.kind === "confirm_asked") return j.name;
    if (p && typeof p === "object") {
      const file = p.file_path ?? p.path;
      if (file) return shortPath(String(file));
      const v = p.query ?? p.title ?? p.filter ?? p.pattern;
      return v ? String(v) : j.text;
    }
  } catch {
    /* не JSON */
  }
  return j.text;
}
const left = (exp: number) => Math.max(0, Math.round((exp * 1000 - Date.now()) / 60000));
</script>

<template>
  <div class="page scroll">
    <section v-if="state.confirmations.length" class="confirms">
      <h3>Нужно подтверждение</h3>
      <div v-for="c in state.confirmations" :key="c.id" class="card confirm">
        <div class="title">{{ c.title }}</div>
        <pre v-if="c.details" class="details">{{ c.details }}</pre>
        <div class="small muted">{{ channelName(c.channel) }} · осталось ~{{ left(c.expires) }} мин</div>
        <div class="actions">
          <button class="primary" @click="resolveConfirmation(c.id, true)">Подтвердить</button>
          <button class="danger" @click="resolveConfirmation(c.id, false)">Отклонить</button>
        </div>
      </div>
    </section>

    <div class="head">
      <h3>Действия агента</h3>
      <button class="small" @click="refreshAll">Обновить</button>
    </div>
    <div v-if="!feed.length" class="empty">Пока пусто. Здесь появятся вызовы инструментов, отказы и решения.</div>
    <div v-for="j in feed" :key="j.id" class="row" :class="j.kind">
      <span class="ic">{{ icon(j.kind) }}</span>
      <div class="body">
        <div>
          <b>{{ j.kind === "tool" || j.kind === "denied" ? toolName(j.name) : kindName(j.kind) }}</b>
          <span class="muted"> · {{ summary(j) }}</span>
        </div>
        <div class="small muted">{{ channelName(j.channel) }} · {{ time(j.ts) }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
h3 { margin: 6px 0; font-size: 15px; }
.head { display: flex; justify-content: space-between; align-items: center; }
.confirms { display: flex; flex-direction: column; gap: 8px; }
.confirm { border-color: var(--warn); }
.title { font-weight: 600; }
.details {
  white-space: pre-wrap;
  font-size: 12px;
  max-height: 160px;
  overflow: auto;
  background: var(--surface-2);
  padding: 8px;
  border-radius: 8px;
  margin: 8px 0;
}
.actions { display: flex; gap: 8px; margin-top: 10px; }
.row { display: flex; gap: 10px; padding: 8px 2px; border-bottom: 1px solid var(--line); }
.row .body { min-width: 0; overflow-wrap: anywhere; }
.row.denied b, .row.error b { color: var(--danger); }
.ic { width: 20px; text-align: center; }
</style>
