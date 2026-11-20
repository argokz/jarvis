<script setup lang="ts">
import { nextTick, ref, watch } from "vue";
import { channelName, time } from "../lib/format";
import { askFast, pttStart, pttStop, sendText, setFast, state, stopSpeaking } from "../lib/state";

const draft = ref("");
const list = ref<HTMLElement>();
let pressedAt = 0;
let toggleMode = false;

async function submit() {
  const text = draft.value.trim();
  if (!text || state.sending) return;
  draft.value = "";
  await (state.fast ? askFast(text) : sendText(text));
}

function scrollDown() {
  void nextTick(() => list.value?.scrollTo({ top: list.value.scrollHeight }));
}
watch(() => [state.messages.length, state.sending, state.voice.phrases.length, state.voice.phase], scrollDown, {
  immediate: true,
});

// Удерживать — говорить; короткое касание — режим «нажал-сказал-нажал».
async function down(e: PointerEvent) {
  (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
  if (state.voice.phase === "recording") {
    toggleMode = false;
    await pttStop();
    return;
  }
  if (state.voice.phase === "speaking" || state.voice.phase === "thinking") stopSpeaking();
  pressedAt = performance.now();
  await pttStart();
}
async function up() {
  if (state.voice.phase !== "recording") return;
  if (performance.now() - pressedAt < 300) {
    toggleMode = true;
    return;
  }
  if (!toggleMode) await pttStop();
}

const micLabel: Record<string, string> = {
  idle: "Нажмите и говорите",
  recording: "Говорите… отпустите, чтобы отправить",
  thinking: "Думаю…",
  speaking: "Отвечаю — нажмите, чтобы перебить",
};
</script>

<template>
  <div class="chat">
    <div ref="list" class="messages scroll">
      <div v-if="!state.messages.length" class="empty">
        Здесь общий разговор из всех каналов: приложение, Telegram и голос.
      </div>
      <div v-for="m in state.messages" :key="m.id" class="msg" :class="m.role">
        <div class="bubble">{{ m.text }}</div>
        <div class="meta">{{ channelName(m.channel) }} · {{ time(m.ts) }}</div>
      </div>
      <div v-if="state.sending" class="msg assistant">
        <div class="bubble typing"><i></i><i></i><i></i></div>
      </div>
      <div v-if="state.voice.phase !== 'idle' && (state.voice.transcript || state.voice.phrases.length)" class="live">
        <div v-if="state.voice.transcript" class="msg user">
          <div class="bubble">{{ state.voice.transcript }}</div>
        </div>
        <div v-if="state.voice.phrases.length" class="msg assistant">
          <div class="bubble">{{ state.voice.phrases.join(" ") }}</div>
        </div>
      </div>
    </div>

    <div v-if="state.voice.error" class="verr" @click="state.voice.error = ''">{{ state.voice.error }}</div>

    <form class="composer" @submit.prevent="submit">
      <textarea
        v-model="draft"
        rows="1"
        placeholder="Сообщение…"
        @keydown.enter.exact.prevent="submit"
      ></textarea>
      <button
        type="button"
        class="fast"
        :class="{ on: state.fast }"
        :aria-pressed="state.fast"
        :title="state.fast ? 'Отвечает быстрая модель и вслух; сложное передаст агенту' : 'Отвечает агент; нажмите — быстрый режим'"
        @click="setFast(!state.fast)"
      >
        ⚡
      </button>
      <button class="primary" type="submit" :disabled="!draft.trim() || state.sending" aria-label="Отправить">➤</button>
      <button
        type="button"
        class="mic"
        :class="state.voice.phase"
        :aria-label="micLabel[state.voice.phase]"
        :title="micLabel[state.voice.phase]"
        @pointerdown.prevent="down"
        @pointerup="up"
        @pointercancel="up"
        @contextmenu.prevent
      >
        {{ state.voice.phase === "speaking" ? "■" : "🎙" }}
      </button>
    </form>
    <div class="hint small muted">
      {{ state.voice.phase === "idle" && state.fast ? "⚡ Быстрый режим: ответ вслух, доли цента" : micLabel[state.voice.phase] }}
    </div>
  </div>
</template>

<style scoped>
.chat { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.messages { flex: 1; padding: 12px 16px; display: flex; flex-direction: column; gap: 10px; }
.msg { display: flex; flex-direction: column; max-width: 85%; }
.msg.user { align-self: flex-end; align-items: flex-end; }
.msg.assistant { align-self: flex-start; }
.bubble {
  padding: 9px 13px;
  border-radius: 16px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  background: var(--surface);
  border: 1px solid var(--line);
}
.user .bubble { background: var(--mine); color: var(--mine-text); border-color: transparent; border-bottom-right-radius: 4px; }
.assistant .bubble { border-bottom-left-radius: 4px; }
.meta { font-size: 11px; color: var(--muted); margin: 2px 4px 0; }
.live { display: flex; flex-direction: column; gap: 10px; opacity: .85; }
.typing { display: flex; gap: 4px; padding: 13px 14px; }
.typing i { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); animation: blink 1.2s infinite; }
.typing i:nth-child(2) { animation-delay: .2s; }
.typing i:nth-child(3) { animation-delay: .4s; }
@keyframes blink { 0%, 80%, 100% { opacity: .25; } 40% { opacity: 1; } }
.verr { margin: 0 16px 6px; color: var(--danger); font-size: 13px; cursor: pointer; }
.composer { display: flex; gap: 8px; padding: 8px 16px 2px; align-items: flex-end; }
.composer textarea { resize: none; max-height: 140px; }
.composer button { height: 44px; min-width: 44px; padding: 0 12px; }
.mic { font-size: 18px; touch-action: none; user-select: none; }
.fast { opacity: .55; }
.fast.on { opacity: 1; border-color: var(--accent); background: color-mix(in srgb, var(--accent) 14%, transparent); }
.mic.recording { background: var(--danger); border-color: var(--danger); color: #fff; animation: pulse 1s infinite; }
.mic.thinking { opacity: .7; }
.mic.speaking { background: var(--accent); border-color: var(--accent); color: #fff; }
@keyframes pulse { 50% { box-shadow: 0 0 0 6px color-mix(in srgb, var(--danger) 30%, transparent); } }
.hint { text-align: center; padding: 0 16px 8px; }
</style>
