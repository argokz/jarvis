<script setup lang="ts">
import { onMounted } from "vue";
import { refreshAll, refreshProjects, state } from "../lib/state";

onMounted(refreshProjects);

const stack = (p: Record<string, unknown>) => (Array.isArray(p.stack) ? (p.stack as string[]).join(", ") : "");
</script>

<template>
  <div class="page scroll">
    <div class="head">
      <h3>Реестр проектов</h3>
      <button class="small" @click="refreshAll">Обновить</button>
    </div>
    <div v-if="!state.projects.length" class="empty">
      Проектов пока нет. Скопируйте 10_Projects/_template.md в vault и заполните frontmatter.
    </div>
    <div v-for="p in state.projects" :key="p.id" class="card">
      <div class="title">
        <b>{{ p.id }}</b>
        <span class="badge" :class="p.status">{{ p.status }}</span>
        <span v-if="p.sensitive" class="badge sensitive">мед. данные</span>
      </div>
      <div v-if="stack(p)" class="small muted">{{ stack(p) }}</div>
      <div class="small">
        Открытых задач: <b>{{ p._open_tasks ?? 0 }}</b>
        <span v-if="p.last_touched" class="muted"> · обновлён {{ p.last_touched }}</span>
      </div>
      <div v-if="p.error" class="small err">{{ p.error }}</div>
    </div>
  </div>
</template>

<style scoped>
h3 { margin: 6px 0; font-size: 15px; }
.head { display: flex; justify-content: space-between; align-items: center; }
.title { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 4px; }
.badge.active { color: var(--ok); }
.badge.paused { color: var(--warn); }
.badge.error, .err { color: var(--danger); }
.badge.sensitive { color: var(--danger); }
</style>
