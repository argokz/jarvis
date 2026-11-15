<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import ChatView from "./components/ChatView.vue";
import ProjectsView from "./components/ProjectsView.vue";
import SettingsView from "./components/SettingsView.vue";
import TasksView from "./components/TasksView.vue";
import { connect, state } from "./lib/state";

type Tab = "chat" | "tasks" | "projects" | "settings";
const tab = ref<Tab>(state.conn.token ? "chat" : "settings");
const tabs: { id: Tab; label: string; icon: string }[] = [
  { id: "chat", label: "Чат", icon: "💬" },
  { id: "tasks", label: "Задачи", icon: "⚡" },
  { id: "projects", label: "Проекты", icon: "📁" },
  { id: "settings", label: "Настройки", icon: "⚙️" },
];
const statusText = computed(
  () => ({ online: "на связи", connecting: "подключаюсь…", offline: "нет связи" })[state.status],
);
onMounted(connect);
</script>

<template>
  <div class="shell">
    <header class="top">
      <strong>Jarvis</strong>
      <span class="status" :class="state.status" :title="state.conn.server">
        <i></i>{{ statusText }}
      </span>
    </header>

    <div v-if="state.confirmations.length && tab !== 'tasks'" class="alert" @click="tab = 'tasks'">
      ⚠️ Нужно подтверждение ({{ state.confirmations.length }}) — открыть
    </div>
    <div v-if="state.error" class="error" role="alert" @click="state.error = ''">{{ state.error }}</div>

    <main>
      <ChatView v-if="tab === 'chat'" />
      <TasksView v-else-if="tab === 'tasks'" />
      <ProjectsView v-else-if="tab === 'projects'" />
      <SettingsView v-else @saved="tab = 'chat'" />
    </main>

    <nav class="tabs">
      <button v-for="t in tabs" :key="t.id" :class="{ active: tab === t.id }" @click="tab = t.id">
        <span class="icon">{{ t.icon }}</span>
        <span>{{ t.label }}</span>
        <b v-if="t.id === 'tasks' && state.confirmations.length" class="dot">{{ state.confirmations.length }}</b>
      </button>
    </nav>
  </div>
</template>

<style scoped>
.shell {
  height: 100%;
  display: flex;
  flex-direction: column;
  max-width: 820px;
  margin: 0 auto;
  padding-top: env(safe-area-inset-top);
}
.top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 16px;
  border-bottom: 1px solid var(--line);
}
.status { font-size: 13px; color: var(--muted); display: flex; align-items: center; gap: 6px; }
.status i { width: 8px; height: 8px; border-radius: 50%; background: var(--danger); }
.status.online i { background: var(--ok); }
.status.connecting i { background: var(--warn); }
.alert, .error { margin: 8px 16px 0; padding: 8px 12px; border-radius: 10px; cursor: pointer; font-size: 14px; }
.alert { background: color-mix(in srgb, var(--warn) 18%, transparent); }
.error { background: color-mix(in srgb, var(--danger) 16%, transparent); }
main { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.tabs {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  border-top: 1px solid var(--line);
  padding-bottom: env(safe-area-inset-bottom);
  background: var(--surface);
}
.tabs button {
  border: 0;
  border-radius: 0;
  background: none;
  padding: 8px 4px 10px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  font-size: 12px;
  color: var(--muted);
  position: relative;
}
.tabs button.active { color: var(--accent); }
.tabs .icon { font-size: 19px; }
.dot {
  position: absolute;
  top: 4px;
  left: calc(50% + 8px);
  background: var(--danger);
  color: #fff;
  border-radius: 999px;
  font-size: 11px;
  padding: 0 6px;
}
</style>
