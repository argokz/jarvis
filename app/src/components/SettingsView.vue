<script setup lang="ts">
import { reactive, ref } from "vue";
import { Api } from "../lib/api";
import { newSession, state, updateConn } from "../lib/state";

const emit = defineEmits<{ saved: [] }>();
const form = reactive({ ...state.conn });
const check = ref("");
const busy = ref(false);

async function save() {
  busy.value = true;
  check.value = "Проверяю…";
  try {
    const probe = new Api(() => form);
    const h = await probe.health();
    await probe.projects(); // проверка токена
    check.value = `✅ Подключено. Модель агента: ${h.model}`;
    updateConn(form);
    emit("saved");
  } catch (e) {
    check.value = `❌ ${e instanceof Error ? e.message : e}`;
  } finally {
    busy.value = false;
  }
}

async function reset() {
  await newSession();
  check.value = "Начат новый разговор. Память в vault сохранена.";
}
</script>

<template>
  <div class="page scroll">
    <form class="card form" @submit.prevent="save">
      <label>
        <span>Адрес ядра</span>
        <input v-model="form.server" placeholder="https://pc.tailnet.ts.net" autocomplete="url" inputmode="url" />
      </label>
      <label>
        <span>Токен (JARVIS_API_TOKEN)</span>
        <input v-model="form.token" type="password" autocomplete="current-password" />
      </label>
      <button class="primary" :disabled="busy || !form.server || !form.token">Сохранить и подключиться</button>
      <div v-if="check" class="small">{{ check }}</div>
    </form>

    <div class="card">
      <b>Разговор</b>
      <p class="small muted">Сессия общая для всех устройств. Новый разговор не стирает память в vault.</p>
      <button :disabled="!state.conn.token" @click="reset">Начать новый разговор</button>
    </div>

    <div class="card small">
      <b>Подсказки</b>
      <ul class="muted">
        <li>С телефона удобнее всего через Tailscale: <code>tailscale serve</code> даёт HTTPS-адрес ядра.</li>
        <li>Микрофон в браузере работает только по HTTPS или на localhost; в приложении — всегда.</li>
        <li>Push-уведомления — приложение ntfy, подписка на тему из NTFY_TOPIC (работает без Google-сервисов).</li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.form { display: flex; flex-direction: column; gap: 12px; }
label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: var(--muted); }
ul { padding-left: 18px; margin: 6px 0 0; }
code { font-size: 12px; }
</style>
