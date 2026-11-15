import { reactive } from "vue";
import { Api, ApiError, type Confirmation, type JournalEvent, type Message, type Project } from "./api";
import { Player, Recorder } from "./audio";
import { type Conn, loadConn, saveConn, wsUrl } from "./config";
import { Reconnecting, type Status } from "./events";
import { mergeMessages } from "./messages";
import { VoiceClient } from "./voice";

export type VoicePhase = "idle" | "recording" | "thinking" | "speaking";

export const state = reactive({
  conn: loadConn() as Conn,
  status: "offline" as Status,
  error: "",
  messages: [] as Message[],
  journal: [] as JournalEvent[],
  confirmations: [] as Confirmation[],
  projects: [] as Project[],
  sending: false,
  voice: { phase: "idle" as VoicePhase, transcript: "", phrases: [] as string[], error: "" },
});

export const api = new Api(() => state.conn);
const player = new Player();
const recorder = new Recorder();


function handleEvent(e: any): void {
  if (e.type === "hello") {
    state.confirmations = e.pending ?? [];
    void refreshMessages();
  } else if (e.type === "journal") {
    const ev = e as JournalEvent;
    state.journal = [ev, ...state.journal.filter((j) => j.id !== ev.id)].slice(0, 300);
    if (ev.kind === "tool" && /Write|Edit/.test(ev.name) && ev.text.includes("10_Projects")) {
      void refreshProjects(); // заметка проекта изменилась — обновляем реестр
    }
    if ((ev.kind === "user" || ev.kind === "assistant") && !ev.channel?.endsWith("-agent") && ev.text) {
      state.messages = mergeMessages(state.messages, [
        { id: ev.id, ts: ev.ts, channel: ev.channel, role: ev.kind, text: ev.text },
      ]);
    }
  } else if (e.type === "confirm") {
    const { type: _t, ...c } = e;
    state.confirmations = [...state.confirmations.filter((x) => x.id !== c.id), c as Confirmation];
  } else if (e.type === "confirm_resolved") {
    state.confirmations = state.confirmations.filter((x) => x.id !== e.id);
  }
}

const events = new Reconnecting(
  () => wsUrl(state.conn, "/events/ws"),
  handleEvent,
  (s) => (state.status = s),
);

const voice = new VoiceClient(() => wsUrl(state.conn, "/voice/ws"), {
  transcript: (t) => (state.voice.transcript = t || "(не расслышал)"),
  phrase: (t) => state.voice.phrases.push(t),
  audio: (pcm, rate) => {
    state.voice.phase = "speaking";
    player.play(pcm, rate);
  },
  error: (t) => (state.voice.error = t),
  done: () => {
    const wait = () => {
      if (player.playing) setTimeout(wait, 200);
      else if (state.voice.phase !== "recording") resetVoice();
    };
    wait();
  },
});

function resetVoice(): void {
  state.voice.phase = "idle";
  state.voice.transcript = "";
  state.voice.phrases = [];
}

async function guard<T>(fn: () => Promise<T>): Promise<T | undefined> {
  try {
    state.error = "";
    return await fn();
  } catch (e) {
    state.error = e instanceof ApiError || e instanceof Error ? e.message : String(e);
    return undefined;
  }
}

export async function refreshMessages(): Promise<void> {
  const list = await guard(() => api.messages(150));
  if (list) state.messages = mergeMessages(state.messages, list);
}

export async function refreshProjects(): Promise<void> {
  const projects = await guard(() => api.projects());
  if (projects) state.projects = projects;
}

export async function refreshAll(): Promise<void> {
  const [journal, projects, confirmations] = await Promise.all([
    guard(() => api.journal(150)),
    guard(() => api.projects()),
    guard(() => api.confirmations()),
  ]);
  if (journal) state.journal = journal;
  if (projects) state.projects = projects;
  if (confirmations) state.confirmations = confirmations;
  await refreshMessages();
}

export function connect(): void {
  if (!state.conn.token) return;
  events.restart();
  void refreshAll();
}

export function updateConn(c: Conn): void {
  state.conn = { server: c.server.trim(), token: c.token.trim() };
  saveConn(state.conn);
  voice.close();
  connect();
}

export async function sendText(text: string): Promise<void> {
  state.sending = true;
  try {
    const r = await guard(() => api.chat(text));
    if (r?.is_error) state.error = r.text;
    await refreshMessages(); // на случай, если лента событий отваливалась
  } finally {
    state.sending = false;
  }
}

export async function resolveConfirmation(id: string, approve: boolean): Promise<void> {
  await guard(() => api.resolve(id, approve));
  state.confirmations = state.confirmations.filter((c) => c.id !== id);
}

export async function newSession(): Promise<void> {
  await guard(() => api.newSession());
}

/** Нажали кнопку микрофона: перебиваем ответ и начинаем запись. */
export async function pttStart(): Promise<void> {
  player.unlock();
  player.stop();
  voice.cancel();
  resetVoice();
  state.voice.error = "";
  try {
    await recorder.start();
    state.voice.phase = "recording";
  } catch (e) {
    state.voice.error = e instanceof Error ? e.message : String(e);
  }
}

/** Отпустили кнопку: отправляем запись. */
export async function pttStop(): Promise<void> {
  if (!recorder.recording) return;
  const { pcm, seconds } = await recorder.stop();
  if (seconds < 0.4) {
    resetVoice();
    return;
  }
  if (seconds > 29) state.voice.error = "Запись обрезана до 29 секунд";
  state.voice.phase = "thinking";
  try {
    await voice.sendAudio(pcm.slice(0, 16000 * 29));
  } catch (e) {
    state.voice.error = e instanceof Error ? e.message : String(e);
    resetVoice();
  }
}

export function stopSpeaking(): void {
  player.stop();
  voice.cancel();
  resetVoice();
}
