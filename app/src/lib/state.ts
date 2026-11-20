import { reactive } from "vue";
import {
  Api, ApiError, type Confirmation, type JournalEvent, type Message, type Project, type Scenario, type Scheduled,
  type FlowStep, type HomeAction, type HomeItem, type HomeOverview, type SpeechCheck, type StatusReport, type Try,
} from "./api";
import { Player, Recorder } from "./audio";
import { type Conn, forgetConn, loadConn, saveConn, storedElsewhere, wsUrl } from "./config";
import { applyItem, type Before, cacheable, fromCache, optimistic, rollback, snapshot } from "./home";
import { desktop, type Service, type ServiceAction } from "./desktop";
import { Reconnecting, type Status } from "./events";
import { mergeMessages } from "./messages";
import { VoiceClient } from "./voice";

export type VoicePhase = "idle" | "recording" | "thinking" | "speaking";
export type Tab = "chat" | "features" | "home" | "tasks" | "scenarios" | "projects" | "settings";

const FAST_KEY = "jarvis.fast";
const HOME_KEY = "jarvis.home";
function loadHome(): HomeOverview | null {
  try {
    return fromCache(localStorage.getItem(HOME_KEY));
  } catch {
    return null;
  }
}
function loadFast(): boolean {
  try {
    return localStorage.getItem(FAST_KEY) === "1";
  } catch {
    return false;
  }
}

export const state = reactive({
  conn: loadConn() as Conn,
  tab: "chat" as Tab,
  role: "owner" as "owner" | "simple", // «простой» профиль: только плитки открытых ему устройств
  assistant: "", // имя ассистента от ядра (у простого профиля сводки /status нет)
  fast: loadFast(), // сообщения из чата отвечает быстрая модель и вслух — как голосу
  report: null as StatusReport | null,
  speechCheck: null as SpeechCheck | null,
  checkingSpeech: false,
  services: [] as Service[],
  home: loadHome(), // до ответа ядра — последний снимок из кэша: вкладка открывается сразу
  homeFresh: false, // снимок уже от ядра, а не из кэша
  homeError: "",
  sheet: "", // устройство, чья карточка открыта (одна на всё приложение)
  homeBusy: "", // служба производителя, которую сейчас опрашиваем
  homePending: {} as Record<string, boolean>, // устройства, чья команда ещё не подтверждена домом
  flow: null as FlowStep | null, // открытый шаг мастера добавления устройства
  flowTitle: "",
  serviceBusy: "",
  autostart: null as boolean | null, // null — оболочки нет или она не умеет автозапуск
  status: "offline" as Status,
  error: "",
  messages: [] as Message[],
  journal: [] as JournalEvent[],
  confirmations: [] as Confirmation[],
  projects: [] as Project[],
  schedule: [] as Scheduled[],
  scenarios: [] as Scenario[],
  scenarioRunning: "",
  scenarioSaid: null as { id: string; text: string } | null,
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
    if (ev.kind === "tool" && ev.name.includes("schedule_")) void refreshSchedule();
    if (ev.kind === "tool" && ev.name.includes("scenario_save")) void refreshScenarios();
    if ((ev.kind === "user" || ev.kind === "assistant") && !ev.channel?.endsWith("-agent") && ev.text) {
      state.messages = mergeMessages(state.messages, [
        { id: ev.id, ts: ev.ts, channel: ev.channel, role: ev.kind, text: ev.text },
      ]);
    }
  } else if (e.type === "confirm") {
    void desktop.showWindow(); // окно могло быть свёрнуто в трей, а вопрос ждёт ответа
    const { type: _t, ...c } = e;
    state.confirmations = [...state.confirmations.filter((x) => x.id !== c.id), c as Confirmation];
  } else if (e.type === "confirm_resolved") {
    state.confirmations = state.confirmations.filter((x) => x.id !== e.id);
  } else if (e.type === "home") {
    // живые состояния: ядро пересылает изменения из Home Assistant
    if (e.reload || !state.home || !applyItem(state.home.items, e.item as HomeItem)) refreshHomeSoon();
    else if (e.item) settle((e.item as HomeItem).entity_id); // дом подтвердил — команда больше не «ждёт»
  } else if (e.type === "health") {
    state.error = String(e.text ?? ""); // сторож ядра: функция перестала отвечать или вернулась
    void refreshStatus();
  } else if (e.type === "alert") {
    if (e.state === "ringing") void desktop.showWindow();
    void refreshSchedule(); // сработало, выключили или отложили — список изменился
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

export async function refreshSchedule(): Promise<void> {
  const schedule = await guard(() => api.schedule());
  if (schedule) state.schedule = schedule;
}

/** Выключить звенящее, отложить на 10 минут или отменить запланированное. */
export async function scheduleAction(id: number, action: "stop" | "snooze" | "cancel"): Promise<void> {
  await guard(() =>
    action === "stop" ? api.scheduleStop(id) : action === "snooze" ? api.scheduleSnooze(id) : api.scheduleCancel(id),
  );
  await refreshSchedule();
}

export async function refreshScenarios(): Promise<void> {
  const scenarios = await guard(() => api.scenarios());
  if (scenarios) state.scenarios = scenarios;
}

/** Запустить сценарий кнопкой; что он сказал — показываем под ним. */
export async function runScenario(id: string): Promise<void> {
  state.scenarioRunning = id;
  state.scenarioSaid = null;
  try {
    const r = await guard(() => api.scenarioRun(id));
    if (r) state.scenarioSaid = { id, text: r.text };
    void refreshSchedule(); // шаг сценария мог поставить таймер или будильник
  } finally {
    state.scenarioRunning = "";
  }
}

export async function enableScenario(id: string, enabled: boolean): Promise<void> {
  const updated = await guard(() => api.scenarioEnable(id, enabled));
  if (updated) state.scenarios = state.scenarios.map((s) => (s.id === id ? updated : s));
  else await refreshScenarios();
}

export async function refreshAll(): Promise<void> {
  const [journal, projects, confirmations] = await Promise.all([
    guard(() => api.journal(150)),
    guard(() => api.projects()),
    guard(() => api.confirmations()),
    refreshSchedule(),
  ]);
  if (journal) state.journal = journal;
  if (projects) state.projects = projects;
  if (confirmations) state.confirmations = confirmations;
  await refreshMessages();
}

export async function refreshStatus(): Promise<void> {
  const report = await guard(() => api.status());
  if (report) state.report = report;
}

/** Синтез фразы и её распознавание обратно — проверка обоих серверов речи без микрофона. */
export async function checkSpeech(): Promise<void> {
  state.checkingSpeech = true;
  try {
    state.speechCheck = (await guard(() => api.speechCheck())) ?? null;
  } finally {
    state.checkingSpeech = false;
  }
}

async function homeCall<T>(fn: () => Promise<T>): Promise<T | undefined> {
  try {
    state.homeError = "";
    return await fn();
  } catch (e) {
    state.homeError = e instanceof Error ? e.message : String(e);
    return undefined;
  }
}

export async function refreshHome(): Promise<void> {
  const home = await homeCall(() => api.home());
  if (!home) return;
  state.home = home;
  state.homeFresh = true;
  try {
    localStorage.setItem(HOME_KEY, cacheable(home));
  } catch {
    /* кэш — удобство, не обязанность */
  }
}

let homeTimer: ReturnType<typeof setTimeout> | undefined;
/** Состав дома изменился или связь вернулась: перечитать снимок, но не чаще раза в треть секунды. */
function refreshHomeSoon(): void {
  clearTimeout(homeTimer);
  homeTimer = setTimeout(() => void refreshHome(), 300);
}

const CONFIRM_MS = 5000; // сколько ждать от дома подтверждения команды
const waiting = new Map<string, { timer: ReturnType<typeof setTimeout>; expect: boolean | null; name: string }>();

function settle(entity: string): void {
  const w = waiting.get(entity);
  if (w) clearTimeout(w.timer);
  waiting.delete(entity);
  const { [entity]: _done, ...rest } = state.homePending;
  state.homePending = rest;
}

/**
 * Команда устройству: строка меняется сразу, команда уходит ядру. Ядро отказало — строка возвращается
 * с причиной. Дом не подтвердил за пять секунд — перечитываем снимок и говорим, если вышло иначе.
 */
export async function homeAct(item: HomeItem, action: HomeAction, value?: number, was?: Before): Promise<void> {
  const entity = item.entity_id;
  const before: Before = was ?? snapshot(item); // was — состояние до протяжки: плитка уже менялась под пальцем
  const expect = optimistic(item, action, value);
  settle(entity);
  state.homePending = { ...state.homePending, [entity]: true };
  try {
    state.homeError = "";
    await api.homeAct(entity, action, value);
  } catch (e) {
    rollback(item, before);
    settle(entity);
    state.homeError = `${item.name}: ${e instanceof Error ? e.message : String(e)}`;
    return;
  }
  const timer = setTimeout(async () => {
    settle(entity);
    await refreshHome();
    const now = state.home?.items.find((i) => i.entity_id === entity);
    if (expect !== null && now && now.on !== expect) state.homeError = `${item.name}: дом не подтвердил команду`;
  }, CONFIRM_MS);
  waiting.set(entity, { timer, expect, name: item.name });
}

/** Название, комната, имена для голоса. Возвращает текст ошибки или пустую строку. */
export async function homeSettings(
  item: HomeItem,
  patch: { name?: string; area_id?: string; new_area?: string; aliases?: string[] },
): Promise<string> {
  try {
    await api.homeSettings(item.entity_id, patch);
  } catch (e) {
    return e instanceof Error ? e.message : String(e);
  }
  await refreshHome();
  return "";
}

/** Своя голосовая команда: фразы и шаги сохраняются сценарием в vault. Возвращает текст ошибки или пустую строку. */
export async function createCommand(name: string, phrases: string[], steps: unknown[]): Promise<string> {
  try {
    await api.scenarioCreate({ name, phrases, steps });
  } catch (e) {
    return e instanceof Error ? e.message : String(e);
  }
  await refreshScenarios();
  return "";
}

export async function scenarioPhrases(id: string, phrases: string[], withoutName: boolean): Promise<string> {
  try {
    const updated = await api.scenarioPhrases(id, phrases, withoutName);
    state.scenarios = state.scenarios.map((s) => (s.id === id ? updated : s));
    return "";
  } catch (e) {
    return e instanceof Error ? e.message : String(e);
  }
}

/** Открыть или закрыть устройство голосовому ассистенту. */
export async function homeExpose(item: HomeItem, exposed: boolean): Promise<void> {
  if (await homeCall(() => api.homeExpose(item.entity_id, exposed))) item.exposed = exposed;
  else await refreshHome();
}

/** Опросить службу производителя заново: так появляются устройства, добавленные в её приложении. */
export async function homeReload(entry: string): Promise<void> {
  state.homeBusy = entry;
  try {
    await homeCall(() => api.homeReload(entry));
    await new Promise((r) => setTimeout(r, 2500)); // служба поднимается и опрашивает облако
    await refreshHome();
  } finally {
    state.homeBusy = "";
  }
}

export async function flowOpen(id: string, title: string): Promise<void> {
  state.flowTitle = title;
  state.flow = (await homeCall(() => api.homeFlow(id))) ?? null;
}

export async function flowAnswer(data: Record<string, unknown>): Promise<void> {
  if (!state.flow) return;
  const next = await homeCall(() => api.homeFlowAnswer(state.flow!.flow_id, data));
  if (next) state.flow = next;
}

export function flowClose(): void {
  state.flow = null;
  void refreshHome();
}

export async function refreshDesktop(): Promise<void> {
  [state.services, state.autostart] = await Promise.all([desktop.services(), desktop.autostart()]);
}

export async function serviceAction(unit: string, action: ServiceAction): Promise<void> {
  state.serviceBusy = unit;
  try {
    state.services = await desktop.serviceAction(unit, action);
  } catch (e) {
    state.error = e instanceof Error ? e.message : String(e);
  } finally {
    state.serviceBusy = "";
  }
  setTimeout(() => void refreshStatus(), 1500); // служба поднимается не мгновенно
}

export async function setAutostart(on: boolean): Promise<void> {
  await desktop.setAutostart(on);
  state.autostart = await desktop.autostart();
}

/**
 * На ПК с ядром оболочка сама знает адрес и токен: вводить их руками не нужно и хранить в окне — тоже.
 * Сохранённое вручную подключение к другому ядру остаётся в силе, пока владелец не попросит локальное (force).
 */
export async function adoptLocal(force = false): Promise<boolean> {
  const local = await desktop.localConn();
  if (!local?.token || (!force && storedElsewhere(local))) return false;
  state.conn = local;
  forgetConn();
  return true;
}

export function connect(): void {
  if (!state.conn.token) return;
  void (async () => {
    const who = await guard(() => api.me()); // по роли решаем, что этому подключению показывать и запрашивать
    state.role = who?.role === "simple" ? "simple" : "owner";
    state.assistant = who?.name ?? "";
    events.restart();
    if (state.role === "simple") {
      void refreshHome(); // остального простому профилю ядро не отдаст
      return;
    }
    void refreshAll();
    void refreshStatus(); // имя ассистента нужно подсказкам на всех вкладках
  })();
}

/** Произнести фразу на колонке. Возвращает текст ошибки или пустую строку. */
export async function homeSay(item: HomeItem, text: string): Promise<string> {
  try {
    await api.homeSay(item.entity_id, text);
    return "";
  } catch (e) {
    return e instanceof Error ? e.message : String(e);
  }
}

/** Кнопка «Позвать» простого профиля: владелец получит уведомление. */
export async function callHelp(): Promise<string> {
  try {
    const r = await api.help();
    return r.repeated ? "Уже позвали — подождите немного." : "Позвали. Сейчас подойдут.";
  } catch (e) {
    return e instanceof Error ? e.message : String(e);
  }
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

export function setFast(on: boolean): void {
  state.fast = on;
  try {
    localStorage.setItem(FAST_KEY, on ? "1" : "0");
  } catch {
    /* не критично */
  }
}

/** Текст быстрой модели — тем же путём, что голос: ответ придёт фразами и вслух, сложное она передаст агенту. */
export async function askFast(text: string): Promise<void> {
  player.unlock();
  player.stop();
  voice.cancel();
  resetVoice();
  state.voice.error = "";
  state.voice.transcript = text;
  state.voice.phase = "thinking";
  try {
    await voice.sendText(text);
  } catch (e) {
    state.voice.error = e instanceof Error ? e.message : String(e);
    resetVoice();
  }
}

/** Проба функции с экрана «Функции»: показываем разговор и отправляем фразу нужной модели. */
export async function tryPhrase(t: Try): Promise<void> {
  state.tab = "chat";
  await (t.via === "fast" ? askFast(t.text) : sendText(t.text));
  void refreshSchedule(); // проба могла поставить таймер
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
