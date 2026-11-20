<script setup lang="ts">
/**
 * «Простой» профиль — для гостей, детей и планшета на стене: крупные плитки того, что владелец открыл,
 * и кнопка «Позвать». Ни настроек, ни карточек устройств, ни разговора с ассистентом.
 */
import { computed, ref } from "vue";
import { groupHome, layoutRooms } from "../lib/home";
import { callHelp, refreshHome, state } from "../lib/state";
import DeviceTile from "./DeviceTile.vue";
import SettingsView from "./SettingsView.vue";

const settings = ref(false);
const called = ref("");
const rooms = computed(() => layoutRooms(groupHome(state.home?.items ?? [], false)));
const statusText = computed(
  () => ({ online: "на связи", connecting: "подключаюсь…", offline: "нет связи" })[state.status],
);

async function help() {
  called.value = "Зову…";
  called.value = await callHelp();
  setTimeout(() => (called.value = ""), 8000);
}
</script>

<template>
  <div class="simple">
    <header>
      <strong>{{ state.assistant || "Дом" }}</strong>
      <span class="status" :class="state.status"><i></i>{{ statusText }}</span>
      <button class="gear" :aria-label="settings ? 'Назад к дому' : 'Подключение'" @click="settings = !settings">
        {{ settings ? "←" : "⚙" }}
      </button>
    </header>

    <SettingsView v-if="settings" @saved="settings = false" />
    <div v-else class="body scroll">
      <div v-if="state.homeError" class="banner" role="alert" @click="state.homeError = ''">{{ state.homeError }}</div>
      <div v-if="!state.home" class="empty">Загружаю дом…</div>
      <div v-else-if="!rooms.length" class="empty">
        Здесь пока пусто. Владелец открывает устройства этому профилю значком 🎙 в карточке устройства.
        <div><button class="small" @click="refreshHome">Обновить</button></div>
      </div>
      <section v-for="r in rooms" :key="r.name">
        <h2>{{ r.name || "Дом" }}</h2>
        <div class="grid">
          <DeviceTile v-for="i in [...r.tiles, ...r.folded.flatMap((g) => g.items)]" :key="i.entity_id" :item="i" plain big />
        </div>
      </section>
    </div>

    <footer v-if="!settings">
      <div v-if="called" class="called" role="status">{{ called }}</div>
      <button class="help" :disabled="called === 'Зову…'" @click="help">🙋 Позвать</button>
    </footer>
  </div>
</template>

<style scoped>
.simple { height: 100%; display: flex; flex-direction: column; padding-top: env(safe-area-inset-top); }
header { display: flex; align-items: center; gap: 12px; padding: 12px 20px; border-bottom: 1px solid var(--line); }
header strong { font-size: 20px; margin-right: auto; }
.status { font-size: 14px; color: var(--muted); display: flex; align-items: center; gap: 6px; }
.status i { width: 9px; height: 9px; border-radius: 50%; background: var(--danger); }
.status.online i { background: var(--ok); }
.status.connecting i { background: var(--warn); }
.gear { min-width: 48px; min-height: 48px; border-radius: 50%; padding: 0; color: var(--muted); }
.body { flex: 1; min-height: 0; padding: 16px 20px; display: flex; flex-direction: column; gap: 22px; }
h2 { margin: 0 0 10px; font-size: 20px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 14px; }
@media (max-width: 479px) { .grid { grid-template-columns: 1fr; } }
.banner { padding: 12px 14px; border-radius: 12px; background: color-mix(in srgb, var(--danger) 16%, transparent); font-size: 16px; }
.empty { font-size: 16px; display: flex; flex-direction: column; gap: 12px; align-items: center; }
footer { padding: 12px 20px calc(14px + env(safe-area-inset-bottom)); border-top: 1px solid var(--line); display: flex; flex-direction: column; gap: 8px; }
.help { min-height: 60px; font-size: 20px; border-radius: 16px; background: var(--surface-2); }
.called { text-align: center; font-size: 16px; }
</style>
