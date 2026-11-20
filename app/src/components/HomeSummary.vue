<script setup lang="ts">
/** Итоги по дому и сценарии — две полосы над плитками. Итог можно нажать: останется только то, что в него вошло. */
import { computed, watch } from "vue";
import { type PillKind, pills } from "../lib/home";
import { runScenario, state } from "../lib/state";

const only = defineModel<PillKind | "">({ default: "" });
const props = defineProps<{ filterable?: boolean }>();
const totals = computed(() => pills(state.home?.items ?? []));
const quick = computed(() => state.scenarios.filter((s) => s.enabled && !s.error).slice(0, 5));
watch(totals, (list) => {
  if (only.value && !list.some((p) => p.kind === only.value)) only.value = ""; // итог исчез — показываем всё
});
const pick = (kind: PillKind) => {
  if (props.filterable) only.value = only.value === kind ? "" : kind;
};
</script>

<template>
  <div v-if="totals.length" class="strip" role="toolbar" aria-label="Итоги по дому">
    <component
      :is="filterable ? 'button' : 'span'"
      v-for="p in totals" :key="p.kind"
      class="pill" :class="[p.kind, { active: only === p.kind, flat: !filterable }]"
      :aria-pressed="filterable ? only === p.kind : undefined"
      @click="pick(p.kind)"
    >
      {{ p.text }}
    </component>
    <button v-if="only" class="pill" @click="only = ''">Показать всё</button>
  </div>
  <div v-if="quick.length" class="strip" aria-label="Сценарии">
    <button
      v-for="s in quick" :key="s.id" class="pill scene"
      :disabled="state.scenarioRunning === s.id" :title="s.phrases.map((p) => `«${p}»`).join(', ')"
      @click="runScenario(s.id)"
    >
      ✨ {{ s.id }}
    </button>
  </div>
</template>

<style scoped>
/* в одну строку; на узком экране прокручивается пальцем */
.strip { display: flex; gap: 8px; overflow-x: auto; scrollbar-width: none; padding-bottom: 2px; flex: none; }
.strip::-webkit-scrollbar { display: none; }
.pill {
  border-radius: 999px; padding: 0 14px; min-height: 40px; font-size: 14px; white-space: nowrap; flex: none;
  display: inline-flex; align-items: center; border: 1px solid var(--line); background: var(--surface);
}
.pill.flat { min-height: 32px; font-size: 13px; padding: 0 12px; }
.pill.active { border-color: var(--accent); background: var(--surface-2); }
.pill.light { color: var(--light); }
.pill.power { color: var(--ok); }
.pill.media, .pill.scene { color: var(--ai); }
.pill.climate { color: var(--cool); }
.pill.offline { color: var(--muted); }
</style>
