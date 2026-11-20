<script setup lang="ts">
/** «Сейчас»: только включённое и то, что пропало со связи. Пустой экран — хороший знак: дом выключен и цел. */
import { computed } from "vue";
import { nowView } from "../lib/home";
import { state } from "../lib/state";
import DeviceTile from "./DeviceTile.vue";

const view = computed(() => nowView(state.home?.items ?? []));
</script>

<template>
  <div class="now">
    <div v-if="!view.active.length && !view.offline.length && !view.busy.length" class="empty">Всё выключено, всё на связи.</div>
    <div v-else-if="!view.active.length && !view.busy.length" class="small muted">Ничего не включено.</div>
    <div v-if="view.active.length" class="grid">
      <DeviceTile v-for="i in view.active" :key="i.entity_id" :item="i" room />
    </div>
    <details v-for="g in view.busy" :key="g.device" class="fold">
      <summary>{{ g.device }} <span class="badge">включено: {{ g.items.length }}</span></summary>
      <div class="grid">
        <DeviceTile v-for="i in g.items" :key="i.entity_id" :item="i" short />
      </div>
    </details>
    <details v-if="view.offline.length" class="fold">
      <summary>Не на связи <span class="badge">{{ view.offline.length }}</span></summary>
      <div class="grid">
        <DeviceTile v-for="i in view.offline" :key="i.entity_id" :item="i" room />
      </div>
    </details>
  </div>
</template>

<style scoped>
.now { display: flex; flex-direction: column; gap: 10px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(156px, 1fr)); gap: 10px; }
@media (max-width: 359px) { .grid { grid-template-columns: 1fr; } }
.fold > summary { cursor: pointer; min-height: 44px; display: flex; align-items: center; gap: 8px; color: var(--muted); }
.fold[open] > summary { margin-bottom: 8px; }
</style>
