<script setup lang="ts">
/**
 * 对话式调研主界面（实施方案第六节的 ChatView）。
 *
 * 中文说明：
 * 这个页面是改造后的会话主体——一条聊天流：
 * 1. 用户气泡 / 助手气泡（流式打字 + 可折叠思考过程）；
 * 2. 论文卡片组、精读报告卡片、综述卡片内嵌在对话流里（按 metadata.kind 分发）；
 * 3. 每个对话回合下面挂一个可折叠的"执行过程"（工具调用运行事件树）；
 * 4. 右侧抽屉展示完整精读报告，底部追问框把问题发回对话流。
 *
 * run 生命周期（提交 → runs → SSE → 聚合 → turn_end 收尾）与
 * 取消/竞态保护完全复用原工作台的成熟模式，聚合逻辑在 SessionStreamAggregator。
 * props/emits 契约与原 SessionWorkspaceView 保持一致，App.vue 无需改动。
 */
import { computed, nextTick, onBeforeUnmount, ref, shallowRef, watch } from "vue";
import { Library, LoaderCircle } from "lucide-vue-next";

import {
  ApiRequestError,
  cancelSessionRun,
  createSession,
  fetchSessionThread,
  startSessionRun,
  subscribeSessionRun,
} from "../api/sessions";
import { fetchPaperReport, fetchWorkspace } from "../api/workspace";
import AssistantBubble from "../components/chat/AssistantBubble.vue";
import MathText from "../components/chat/MathText.vue";
import ChatComposer from "../components/chat/ChatComposer.vue";
import DeepReadReportCard from "../components/chat/DeepReadReportCard.vue";
import DeepReadDrawer from "../components/chat/DeepReadDrawer.vue";
import PaperCardGroup from "../components/chat/PaperCardGroup.vue";
import PaperInfoDialog from "../components/chat/PaperInfoDialog.vue";
import ReviewMessage from "../components/chat/ReviewMessage.vue";
import ToolCallTrace from "../components/chat/ToolCallTrace.vue";
import UserBubble from "../components/chat/UserBubble.vue";
import StatusPill from "../components/StatusPill.vue";
import PaperLibraryPanel from "../components/workspace/PaperLibraryPanel.vue";
import { createReadableId } from "../lib/random-id";
import { SessionStreamAggregator } from "../lib/session-stream-aggregator";
import { pushToast } from "../stores/notifications";
import type {
  ChatPaperCard,
  DeepReadCardPayload,
  DeepReadReportPayload,
  PaperListPayload,
  ReviewCardPayload,
  WorkspacePaperItem,
} from "../types/chat";
import type {
  SessionRuntimeEvent,
  SessionSummary,
  SessionThread,
  SessionTimelineSnapshot,
  UISessionMessage,
  UIRuntimeTimelineEvent,
} from "../types/sessions";

const props = defineProps<{
  sessions: SessionSummary[];
  selectedKey: string;
  creatingSession: boolean;
}>();

const emit = defineEmits<{
  "update:selectedKey": [sessionKey: string];
  refreshSessions: [];
}>();

// ---------------------------------------------------------------------------
// 会话与运行状态（沿用原工作台的状态机）
// ---------------------------------------------------------------------------

const selectedSessionKey = ref("");
const selectedTitle = ref("新的调研对话");
const draft = ref("");
const threadLoading = ref(false);
const sending = ref(false);
const cancelling = ref(false);
const activeRunId = ref<string | null>(null);
const streamSource = ref<EventSource | null>(null);
const manualClose = ref(false);
const timelineSnapshot = shallowRef<SessionTimelineSnapshot | null>(null);
const scrollElement = ref<HTMLElement | null>(null);
// 中文说明：旧会话可能有十几轮对话、上百张论文卡。打开时先只挂最近几轮，
// 上面藏起来的轮数记在 hiddenTurnCount 里；要点「加载更早的对话」才会把更早的挂上来。
const INITIAL_VISIBLE_TURNS = 3;
const OLDER_TURN_BATCH = 4;
const hiddenTurnCount = ref(0);
let loadingOlderTurns = false;
// 中文注释：用户是否手动向上滚动过（离开底部）。流式输出时如果用户向上滚了，
// 就不自动滚到底部，直到用户点击「回到底部」按钮。
const userScrolledUp = ref(false);
// 中文注释：是否显示「回到底部」按钮。
const showScrollToBottom = ref(false);
// 中文注释：SSE 断线重连的尝试次数。每次成功重连后重置为 0。
// 指数退避：1s / 2s / 4s / 8s / 16s，最多 5 次。
const reconnectAttempts = ref(0);
const MAX_RECONNECT_ATTEMPTS = 5;
const STALE_STREAM_CHECK_MS = 8000;
const TERMINAL_SESSION_STATUSES = new Set(["completed", "failed", "cancelled", "interrupted"]);

function isTerminalSessionStatus(status: string | null | undefined): boolean {
  return Boolean(status && TERMINAL_SESSION_STATUSES.has(status));
}

// ---------------------------------------------------------------------------
// 右栏（论文工作区）宽度：用户可以拖拽调整，拖完的宽度会记在浏览器里
// ---------------------------------------------------------------------------

const PANEL_WIDTH_DEFAULT = 320;
const PANEL_WIDTH_MIN = 260;
const PANEL_WIDTH_MAX = 480;

const panelWidth = ref(readStoredPanelWidth());
const resizing = ref(false);
// 中文说明：窄屏下右栏是盖在界面上的浮层，这个状态控制它是展开还是收起。
// 宽屏时右栏一直在，这个状态不起作用（按钮也被 CSS 藏起来了）。
const narrowPanelOpen = ref(false);

// 中文说明：右栏只在「选了会话、且不在欢迎页」时才显示。
const panelVisible = computed(() => Boolean(selectedSessionKey.value) && !showWelcome.value);

// 中文说明：这里只把宽度交给 CSS 变量。分成几栏由 CSS 根据 data-panel 标记去决定，
// 这样窄屏的断点规则才能覆盖它 —— 内联样式会压过媒体查询，写在 style 里就压不动了。
const layoutStyle = computed(() =>
  panelVisible.value ? { "--panel-w": `${panelWidth.value}px` } : {},
);

/** 中文说明：读取上次存的右栏宽度。读出来不对劲（比如被手动改坏了）就回到默认值。 */
function readStoredPanelWidth() {
  const raw = Number(localStorage.getItem("pa.panel.width"));
  if (!Number.isFinite(raw) || raw < PANEL_WIDTH_MIN || raw > PANEL_WIDTH_MAX) {
    return PANEL_WIDTH_DEFAULT;
  }
  return raw;
}

/** 中文说明：拖拽右栏宽度。
 *  按下把手时先记住鼠标起点和当时的宽度；鼠标移动时按位移算出新宽度；
 *  松开鼠标时把最终宽度存进浏览器，下次打开还是这个宽度。 */
function startPanelResize(event: PointerEvent) {
  const startX = event.clientX;
  const startWidth = panelWidth.value;
  resizing.value = true;
  // 中文说明：拖拽期间给 body 打个标记，让页面上的文字不会被顺手刷成蓝色。
  document.body.setAttribute("data-resizing", "true");

  const handleMove = (moveEvent: PointerEvent) => {
    // 中文说明：鼠标往左拖是「把右栏拉宽」，所以要反过来减。
    const next = startWidth - (moveEvent.clientX - startX);
    panelWidth.value = Math.min(PANEL_WIDTH_MAX, Math.max(PANEL_WIDTH_MIN, next));
  };

  const handleUp = () => {
    window.removeEventListener("pointermove", handleMove);
    window.removeEventListener("pointerup", handleUp);
    resizing.value = false;
    document.body.removeAttribute("data-resizing");
    localStorage.setItem("pa.panel.width", String(panelWidth.value));
  };

  window.addEventListener("pointermove", handleMove);
  window.addEventListener("pointerup", handleUp);
}

/** 中文说明：双击把手，把右栏宽度恢复成默认的 320px。 */
function resetPanelWidth() {
  panelWidth.value = PANEL_WIDTH_DEFAULT;
  localStorage.setItem("pa.panel.width", String(PANEL_WIDTH_DEFAULT));
}

// 精读报告抽屉状态
const drawerVisible = ref(false);
const drawerReport = ref<DeepReadReportPayload | null>(null);
// 当前会话里已有精读报告的论文编号（论文卡片上的"报告"按钮靠它显示）
const readablePaperIds = ref<Set<string>>(new Set());
// 当前会话工作区里全部论文编号（助手回复里的 [paper_id] 引用按钮靠它判定）
const workspacePaperIds = ref<Set<string>>(new Set());
// 当前会话工作区的论文明细（点 [paper_id] 引用、且这篇还没精读时，弹窗用它展示标题和摘要）
const workspacePapers = ref<Map<string, WorkspacePaperItem>>(new Map());
// 论文信息弹窗状态：点击引用但该论文尚未精读时弹出
const paperDialogVisible = ref(false);
const dialogPaper = ref<WorkspacePaperItem | null>(null);

// 论文工作区面板引用（用于 turn_end 时刷新）
const libraryPanelRef = ref<InstanceType<typeof PaperLibraryPanel> | null>(null);

const aggregator = new SessionStreamAggregator();

const selectedSummary = computed(() => props.sessions.find((session) => session.key === selectedSessionKey.value));

watch(selectedSummary, (summary) => {
  if (summary?.title) {
    selectedTitle.value = summary.title;
  }
});

watch(
  () => selectedSummary.value?.status,
  async (status, previous) => {
    if (!status || status === previous || !isTerminalSessionStatus(status)) {
      return;
    }
    if (!timelineSnapshot.value?.isStreaming && !hasRunningRuntimeCards()) {
      return;
    }
    try {
      await reloadCurrentThread();
    } catch {
      syncSnapshot();
    }
  },
);

const currentStatus = computed(() => {
  const listed = selectedSummary.value?.status;
  // 中文注释：侧栏已经从接口拿到终态，但 SSE 漏了 turn_end 时，本地快照仍可能是 running。
  // 状态点以会话记录为准，避免标题写「已完成」输入框却还锁着。
  if (listed && isTerminalSessionStatus(listed)) {
    return listed;
  }
  return timelineSnapshot.value?.status ?? listed ?? "created";
});
const isRunning = computed(() => {
  const listed = selectedSummary.value?.status;
  if (listed && isTerminalSessionStatus(listed)) {
    return false;
  }
  return timelineSnapshot.value?.isStreaming ?? false;
});
const hasMessages = computed(() => (timelineSnapshot.value?.messages.length ?? 0) > 0);

/** 欢迎态：没有选中会话或会话里还没有任何消息时，显示居中的大输入框。 */
const showWelcome = computed(() => !threadLoading.value && !hasMessages.value && !isRunning.value);

const statusText = computed(() => {
  if (cancelling.value || currentStatus.value === "cancel_requested") return "正在停止";
  if (sending.value) return "正在连接";
  if (isRunning.value) return "助手工作中";
  return "准备就绪";
});

const taskStatusLabel = computed(() => {
  if (currentStatus.value === "completed") return "已完成";
  if (currentStatus.value === "running") return "进行中";
  if (currentStatus.value === "cancel_requested") return "正在停止";
  if (currentStatus.value === "cancelled") return "已停止";
  if (currentStatus.value === "failed") return "执行失败";
  if (currentStatus.value === "interrupted") return "已中断";
  return "等待开始";
});

// ---------------------------------------------------------------------------
// 对话流渲染：把消息和运行事件按对话回合分组
// ---------------------------------------------------------------------------

interface FlowTurn {
  turnId: string | null;
  messages: UISessionMessage[];
  events: UIRuntimeTimelineEvent[];
}

/**
 * 按 turnId 把消息和运行事件组织成"回合"列表。
 * 每个回合渲染为：用户气泡 → 助手气泡/卡片（按时间序）→ 执行过程折叠区。
 */
const flowTurns = computed<FlowTurn[]>(() => {
  const snapshot = timelineSnapshot.value;
  if (!snapshot) return [];
  const turns: FlowTurn[] = [];
  const byTurn = new Map<string | null, FlowTurn>();
  for (const message of snapshot.messages) {
    const key = message.turnId ?? null;
    let turn = byTurn.get(key);
    if (!turn) {
      turn = { turnId: key, messages: [], events: [] };
      byTurn.set(key, turn);
      turns.push(turn);
    }
    turn.messages.push(message);
  }
  for (const event of snapshot.runtimeEvents) {
    const key = typeof event.raw?.turn_id === "string" ? event.raw.turn_id : null;
    const turn = byTurn.get(key) ?? turns[turns.length - 1];
    if (turn) {
      turn.events.push(event);
    }
  }
  return turns;
});

/** 真正挂到页面上的回合：跳过开头藏起来的那些更早对话。 */
const visibleFlowTurns = computed(() => flowTurns.value.slice(hiddenTurnCount.value));

/**
 * 当前回合还没有正在打字的助手气泡时，先占一张「正在组织回复…」。
 * 第一段思考/正文到达后会换成真正的流式气泡，避免发送后长时间空白。
 */
function turnNeedsStreamingPlaceholder(turn: FlowTurn, isLastVisible: boolean): boolean {
  if (!isLastVisible || !isRunning.value) {
    return false;
  }
  return !turn.messages.some((message) => message.role === "assistant" && message.isStreaming);
}

/** 上面是否还有更早的对话可以加载。 */
const hasOlderTurns = computed(() => hiddenTurnCount.value > 0);

/** 卡片载荷的类型收窄辅助（模板里按 kind 分发后做一次性断言）。 */
function asPaperList(message: UISessionMessage): PaperListPayload | null {
  return (message.card as PaperListPayload | null) ?? null;
}
function asDeepRead(message: UISessionMessage): DeepReadCardPayload | null {
  return (message.card as DeepReadCardPayload | null) ?? null;
}
function asReview(message: UISessionMessage): ReviewCardPayload | null {
  return (message.card as ReviewCardPayload | null) ?? null;
}

// ---------------------------------------------------------------------------
// 会话切换与历史恢复
// ---------------------------------------------------------------------------

watch(
  () => props.selectedKey,
  async (sessionKey) => {
    if (!sessionKey) {
      resetToBlankWorkspace();
      return;
    }
    await selectSession(sessionKey);
  },
  { immediate: true },
);

onBeforeUnmount(() => {
  closeStream(true);
  // 中文说明：组件卸载时把排队中的渲染取消掉，免得它下一帧去动已经销毁的组件状态。
  cancelScheduledSnapshot();
});

/** 加载指定会话的线程快照并重建聊天流（刷新恢复走的就是这里）。 */
async function selectSession(sessionKey: string) {
  if (!sessionKey || sessionKey === selectedSessionKey.value) {
    return;
  }

  closeStream(true);
  activeRunId.value = null;
  cancelling.value = false;
  drawerVisible.value = false;
  drawerReport.value = null;
  selectedSessionKey.value = sessionKey;
  threadLoading.value = true;
  // 中文说明：先把上一份对话从页面上卸掉，切到很长的旧会话时就不会两份历史叠在一起画。
  timelineSnapshot.value = null;
  hiddenTurnCount.value = 0;
  userScrolledUp.value = false;
  showScrollToBottom.value = false;
  try {
    const thread = await fetchSessionThread(sessionKey);
    // 中文注释：用户连续点击多个历史会话时，旧请求可能比新请求更晚返回；直接丢掉旧结果。
    if (selectedSessionKey.value !== sessionKey) {
      return;
    }
    hydrateThread(thread);
    await refreshWorkspacePapers();
  } catch (error) {
    if (error instanceof ApiRequestError && error.status === 404 && selectedSessionKey.value === sessionKey) {
      resetToBlankWorkspace();
      emit("update:selectedKey", "");
      emit("refreshSessions");
      handleError(error, "会话不存在，已回到空白页");
      return;
    }
    handleError(error, "加载会话历史失败");
  } finally {
    if (selectedSessionKey.value === sessionKey) {
      threadLoading.value = false;
    }
  }
  // 中文说明：等「正在恢复」提示关掉、回合真正画到页面上之后，再决定露出几轮。
  // 否则量高度时还夹着加载提示，容易把该藏起来的旧对话一次全补出来。
  if (selectedSessionKey.value === sessionKey && timelineSnapshot.value) {
    resetTurnWindow();
  }
}

/** 没有任何会话时回到初始欢迎态。 */
function resetToBlankWorkspace() {
  closeStream(true);
  selectedSessionKey.value = "";
  selectedTitle.value = "新的调研对话";
  timelineSnapshot.value = null;
  sending.value = false;
  cancelling.value = false;
  activeRunId.value = null;
  threadLoading.value = false;
  hiddenTurnCount.value = 0;
  userScrolledUp.value = false;
  showScrollToBottom.value = false;
  drawerVisible.value = false;
  drawerReport.value = null;
  readablePaperIds.value = new Set();
}

/** 用线程快照重建聚合器状态，并在有活跃 run 时自动接回实时流。

    中文注释：
    改造前刷新页面后 activeRunId 被重置为 null，用户必须手动重启调研。
    现在后端把 run_id 写进 session metadata，thread 接口会带回来。
    如果会话还在 running 且有 active_run_id，前端直接 openStream 接回去。
    后端 broker 的 subscribe 会补发内存里的全部历史事件，所以刷新不会丢内容。
*/
function hydrateThread(thread: SessionThread) {
  selectedTitle.value = thread.title || "调研对话";
  aggregator.hydrate(thread);
  syncSnapshot();

  // 中文注释：如果后端报告这个会话还有活跃的 run，自动接回实时流。
  if (thread.status === "running" && thread.active_run_id) {
    activeRunId.value = thread.active_run_id;
    sending.value = false;
    const streamUrl = `/api/sessions/${encodeURIComponent(selectedSessionKey.value)}/runs/${encodeURIComponent(thread.active_run_id)}/stream`;
    openStream(selectedSessionKey.value, streamUrl);
  }
}

// ---------------------------------------------------------------------------
// 快照渲染的合并
// ---------------------------------------------------------------------------

/* 中文说明：流式输出时事件非常密集（一次精读实测能推几十条进度事件）。
   如果每条事件都立刻把整个快照渲染一遍，整页会跟着反复重画，
   看起来就像「每次模型或工具一出东西，页面就整个刷新一次」。

   这里把一个动画帧之内的多条事件合并成一次渲染 —— 中间态不显示，
   最终状态和以前完全一样，但渲染次数大幅下降。

   注意：hydrateThread 和 submitMessage 里调的是 syncSnapshot()（不合并），
   因为它们各自代表一次用户可见的完整动作（切换会话、发出消息），必须马上看到结果。 */
let pendingSnapshotFrame = 0;
let staleStreamCheckTimer = 0;

/** 把下一次快照渲染排到当前这一帧的末尾。同一帧里重复调用只会排一次。 */
function scheduleSnapshot() {
  if (pendingSnapshotFrame) {
    return;
  }
  pendingSnapshotFrame = requestAnimationFrame(() => {
    pendingSnapshotFrame = 0;
    syncSnapshot();
  });
}

/** 取消排队中、还没执行的那次合并渲染。 */
function cancelScheduledSnapshot() {
  if (pendingSnapshotFrame) {
    cancelAnimationFrame(pendingSnapshotFrame);
    pendingSnapshotFrame = 0;
  }
}

/** 把聚合器快照写回响应式状态。只有在用户没有手动向上滚动时才自动滚到底部。 */
function syncSnapshot() {
  // 中文说明：这里要立刻生效，所以先把排队中的合并渲染取消掉 ——
  // 否则它下一帧会拿一份旧快照，把刚写进去的新结果覆盖回去。
  cancelScheduledSnapshot();
  timelineSnapshot.value = aggregator.snapshot();
  // 中文注释：流式输出时如果用户向上滚了想回看前面的内容，就不强制滚到底部。
  if (!userScrolledUp.value) {
    scrollToBottom();
  }
}

/** 拉取工作区快照，维护"哪些论文已有精读报告"（卡片上的报告按钮）。 */
async function refreshWorkspacePapers() {
  if (!selectedSessionKey.value) {
    readablePaperIds.value = new Set();
    workspacePaperIds.value = new Set();
    workspacePapers.value = new Map();
    return;
  }
  try {
    const snapshot = await fetchWorkspace(selectedSessionKey.value);
    const nextReadable = snapshot.papers.filter((paper) => paper.has_report).map((paper) => paper.paper_id);
    const nextIds = snapshot.papers.map((paper) => paper.paper_id);
    // 中文注释：Set 引用变了会让所有助手气泡重解析 Markdown。成员没变时复用旧集合。
    if (!sameStringSet(readablePaperIds.value, nextReadable)) {
      readablePaperIds.value = new Set(nextReadable);
    }
    if (!sameStringSet(workspacePaperIds.value, nextIds)) {
      workspacePaperIds.value = new Set(nextIds);
    }
    // 中文注释：同时按编号存一份论文明细，点引用但还没精读时用它弹出论文信息卡片。
    workspacePapers.value = new Map(snapshot.papers.map((paper) => [paper.paper_id, paper]));
  } catch {
    // 中文注释：工作区接口失败不阻塞聊天主流程，卡片只是暂时少一个"报告"按钮。
  }
}

/** 自动滚到底部（新内容到来时保持跟随）。 */
function scrollToBottom() {
  nextTick(() => {
    const element = scrollElement.value;
    if (element) {
      element.scrollTop = element.scrollHeight;
    }
  });
}

/** 判断用户是否在底部附近（距离底部不超过阈值）。 */
function isNearBottom(): boolean {
  const element = scrollElement.value;
  if (!element) return true;
  const threshold = 100; // 距离底部 100px 以内视为「在底部」
  return element.scrollHeight - element.scrollTop - element.clientHeight < threshold;
}

/** 滚动事件处理：检测用户是否手动向上滚动。 */
function handleScroll() {
  const nearBottom = isNearBottom();
  userScrolledUp.value = !nearBottom;
  showScrollToBottom.value = !nearBottom;
}

/** 打开会话时只露出最近几轮；如果还撑不满一屏，就再补几轮，避免短对话被藏在上面点不到。 */
function resetTurnWindow() {
  hiddenTurnCount.value = Math.max(0, flowTurns.value.length - INITIAL_VISIBLE_TURNS);
  nextTick(() => fillViewportWithTurns());
}

/** 如果当前画出的回合还不够一屏高，继续把更早的对话补上来。 */
function fillViewportWithTurns() {
  const element = scrollElement.value;
  if (!element) {
    return;
  }
  // 中文说明：对话区还没量出高度时不要往上补回合，否则会把全部历史一次挂上去。
  if (element.clientHeight < 8) {
    scrollToBottom();
    return;
  }
  if (hiddenTurnCount.value <= 0 || element.scrollHeight > element.clientHeight + 24) {
    scrollToBottom();
    return;
  }
  hiddenTurnCount.value = Math.max(0, hiddenTurnCount.value - OLDER_TURN_BATCH);
  nextTick(fillViewportWithTurns);
}

/** 再挂一批更早的回合，并停在这批新内容的开头，方便接着往前读。 */
function loadOlderTurns() {
  if (hiddenTurnCount.value <= 0 || loadingOlderTurns) {
    return;
  }
  loadingOlderTurns = true;
  hiddenTurnCount.value = Math.max(0, hiddenTurnCount.value - OLDER_TURN_BATCH);
  // 中文说明：用户是来看更早的对话的，不要把滚动条补回刚才的位置，
  // 更不要弹回最底下。停在新挂上的那几轮开头，才能接着往前读。
  userScrolledUp.value = true;
  showScrollToBottom.value = true;
  nextTick(() => {
    const element = scrollElement.value;
    if (element) {
      element.scrollTop = 0;
    }
    loadingOlderTurns = false;
  });
}

/** 手动点击「回到底部」按钮。 */
function scrollToBottomManual() {
  scrollToBottom();
  userScrolledUp.value = false;
  showScrollToBottom.value = false;
}

/** 重试本轮：找到本轮的用户消息并重新发送。 */
function retryTurn(turnId: string | null) {
  if (!turnId) return;
  const turn = flowTurns.value.find((t) => t.turnId === turnId);
  if (!turn) return;
  const userMessage = turn.messages.find((m) => m.role === "user");
  if (!userMessage) return;
  // 重新发送用户消息。
  submitMessage(userMessage.content);
}

// ---------------------------------------------------------------------------
// 发送 / 流式接收 / 取消
// ---------------------------------------------------------------------------

/** 提交一条消息（输入框发送、卡片"精读"、抽屉追问都走这里）。
 *
 * 中文注释：resumeThreadId 有值时表示这次不是普通发言，而是"接着写上次没写完的
 * 综述"——后端会拿这个编号去读检查点，从最后一个做完的小节往后写。内容还是照常
 * 发一条用户消息，这样对话记录里能看到用户点了继续。
 */
async function submitMessage(presetContent?: string, resumeThreadId?: string) {
  const content = (presetContent ?? draft.value).trim();
  if (!content || sending.value || isRunning.value) {
    return;
  }

  sending.value = true;
  const submittedContent = content;
  draft.value = "";

  try {
    const sessionKey = await ensureActiveSession();
    const turnId = createReadableId();
    aggregator.addOptimisticUserMessage(submittedContent, turnId);
    syncSnapshot();
    const accepted = await startSessionRun(sessionKey, {
      content: submittedContent,
      turn_id: turnId,
      // 中文注释：只在真的续跑时才带上这个字段，普通发言不带，免得后端误判。
      ...(resumeThreadId ? { resume_review_thread: resumeThreadId } : {}),
    });
    emit("refreshSessions");
    activeRunId.value = accepted.run_id;
    openStream(sessionKey, accepted.stream_url);
  } catch (error) {
    draft.value = submittedContent;
    await reloadCurrentThread();
    handleError(error, "发送消息失败");
    sending.value = false;
  }
}

/** 用户点了失败/停止卡片上的"继续"：带着续跑编号重新发一条消息。 */
function resumeReview(threadId: string) {
  submitMessage("继续生成上次没写完的综述", threadId);
}

/** 中文注释：正在跑的时候不能再发起新运行（后端也会 409 拒绝），所以这时候不往下传
 *  回调，卡片上的"继续"按钮就不会出现，避免点了没反应。 */
const resumeHandler = computed(() => (isRunning.value ? undefined : resumeReview));

/** 为实时流建立 EventSource 订阅，每条事件交给聚合器整理。 */
function openStream(sessionKey: string, streamUrl: string) {
  closeStream(true);
  manualClose.value = false;
  reconnectAttempts.value = 0;
  streamSource.value = subscribeSessionRun(streamUrl, {
    onOpen: () => {
      sending.value = false;
    },
    onEvent: async (event) => {
      const changed = aggregator.apply(event);
      // 中文说明：不在这里直接渲染，而是排到本帧末尾统一渲染一次，
      // 把密集的流式事件合并掉（详见 scheduleSnapshot 的注释）。
      // 已经应用过的 SSE 重放、重复的用户回声返回 false，这时不要动快照，
      // 否则气泡和工具卡片会跟着整段重绘，动画被掐断。
      if (changed) {
        scheduleSnapshot();
      }
      // 中文注释：检索工具一执行完就会推一条 kind=paper_list 的消息，这时论文已经写进工作区。
      // 顺手让左侧论文面板重拉一次，用户不用刷新页面就能看到刚检索到的论文。
      const cardKind = typeof event.metadata?.kind === "string" ? event.metadata.kind : "";
      if (event.event === "message" && cardKind === "paper_list") {
        void libraryPanelRef.value?.refresh();
        void refreshWorkspacePapers();
      }
      if (event.event === "turn_end") {
        await handleRunFinished(sessionKey, event);
      }
    },
    onError: async (event) => {
      if (manualClose.value) {
        return;
      }
      const source = event.target as EventSource | null;
      // 中文注释：浏览器 EventSource 断线后会把 readyState 设成 CONNECTING 并自己重连。
      // 心跳闪断不要整段 hydrate。但若后端其实已经跑完，这条连接可能一直停在
      // CONNECTING，界面就会永远锁在「处理中」。过几秒去对一下线程快照。
      if (source && source.readyState !== EventSource.CLOSED) {
        scheduleStaleStreamCheck(sessionKey);
        return;
      }
      // 中文注释：连接已经被关掉（不是浏览器正在重连）才走下面的退避。
      if (reconnectAttempts.value >= MAX_RECONNECT_ATTEMPTS) {
        closeStream(true);
        sending.value = false;
        reconnectAttempts.value = 0;
        pushToast({
          tone: "error",
          title: "实时连接已断开",
          description: "多次重连失败，请刷新页面或手动重试。",
        });
        // 中文注释：后端可能还没恢复，这个快照请求随时会失败。必须用 try/catch
        // 接住异常，否则它会变成没人处理的 promise 拒绝，把后面的收尾步骤
        // （刷新会话列表）也一起掐断。拉不到快照就先沿用界面上已有的状态。
        try {
          await reloadCurrentThread();
        } catch {
          // 快照暂时拿不到，不影响收尾流程。
        }
        emit("refreshSessions");
        return;
      }

      const delay = Math.min(1000 * 2 ** reconnectAttempts.value, 16000);
      reconnectAttempts.value += 1;

      closeStream(true);
      await new Promise((resolve) => setTimeout(resolve, delay));

      // 中文注释：延迟结束后，重新拉一次线程快照。如果 run 还在 running，就接回去；
      // 如果已经结束了（turn_end 在我们断线期间到达），reloadCurrentThread 会把
      // 状态刷成 completed/failed，不再尝试重连。
      // 这个请求在后端还没起来时必然失败，必须用 try/catch 接住：异常如果漏出去
      // 会变成未处理的 promise 拒绝，整条"退避 → 重连"的链条就在这里断掉，
      // 界面永远停在转圈状态。拉不到快照就当"快照暂时不可用"，沿用当前界面上
      // 的状态继续走后面的重试判断。
      try {
        await reloadCurrentThread();
      } catch {
        // 快照暂时拿不到，不中断重连流程。
      }
      emit("refreshSessions");

      if (timelineSnapshot.value?.isStreaming && activeRunId.value) {
        const streamUrl = `/api/sessions/${encodeURIComponent(selectedSessionKey.value)}/runs/${encodeURIComponent(activeRunId.value)}/stream`;
        openStream(selectedSessionKey.value, streamUrl);
      } else {
        reconnectAttempts.value = 0;
      }
    },
  });
}

/** 向后端发送真正的停止请求（只关 SSE 不会停掉后台任务）。 */
async function cancelActiveRun() {
  const runId = activeRunId.value;
  if (!selectedSessionKey.value || !runId || cancelling.value) {
    return;
  }

  cancelling.value = true;
  try {
    await cancelSessionRun(selectedSessionKey.value, runId);
  } catch (error) {
    cancelling.value = false;
    handleError(error, "停止任务失败");
  }
}

/** run 结束：工作区和侧栏会话列表可能已经变了，把它们同步过来。

    中文注释：对话流本身已经由 SSE 事件聚合完毕，这里不再整段 hydrate。
    以前每次 turn_end 都重新拉线程，消息会被换上新的随机 id，Vue 把气泡和
    工具卡片全部拆掉重挂，结束瞬间会闪一下。 */
async function handleRunFinished(sessionKey: string, event: SessionRuntimeEvent) {
  closeStream(true);
  sending.value = false;
  cancelling.value = false;
  activeRunId.value = null;
  // 中文注释：结束通知到达时，中间的工具完成事件可能已经被 stream_seq 去重丢掉。
  // 消息 id 已经稳定，这里重新拉线程只会把漏掉的卡片状态补齐，不会拆掉整页。
  try {
    await reloadCurrentThread();
  } catch {
    syncSnapshot();
  }
  await refreshWorkspacePapers();
  // 中文注释：一轮跑完后工作区可能已经变了（新检索到论文、评价出分、精读完成），
  // 让左侧论文面板也跟着刷新一次，否则用户要手动刷新页面才能看到最新状态。
  void libraryPanelRef.value?.refresh();
  emit("refreshSessions");
  if (event.status === "failed") {
    pushToast({
      tone: "error",
      title: "本轮执行失败",
      description: event.message ?? event.content ?? "请查看对话流中的错误信息。",
    });
    return;
  }
  if (event.status === "cancelled") {
    pushToast({ tone: "info", title: "已停止", description: "已保留停止前完成的结果。" });
    return;
  }
  if (selectedSessionKey.value !== sessionKey) {
    emit("update:selectedKey", sessionKey);
  }
}

/** 若当前还没有活动会话，自动创建一个再继续提交。 */
async function ensureActiveSession() {
  if (selectedSessionKey.value) {
    return selectedSessionKey.value;
  }
  const payload = await createSession();
  hydrateThread(emptyThreadFromSummary(payload.session));
  selectedSessionKey.value = payload.session.key;
  emit("update:selectedKey", payload.session.key);
  emit("refreshSessions");
  return payload.session.key;
}

/** 关闭当前 EventSource，避免切换会话后仍消费旧流。 */
function closeStream(markAsManual: boolean) {
  manualClose.value = markAsManual;
  if (staleStreamCheckTimer) {
    window.clearTimeout(staleStreamCheckTimer);
    staleStreamCheckTimer = 0;
  }
  if (streamSource.value) {
    streamSource.value.close();
    streamSource.value = null;
  }
}

/** SSE 还在自己重连时，过几秒去对一次线程快照；后端已经结束就补齐卡片并解锁输入。 */
function scheduleStaleStreamCheck(sessionKey: string) {
  if (staleStreamCheckTimer || manualClose.value) {
    return;
  }
  staleStreamCheckTimer = window.setTimeout(async () => {
    staleStreamCheckTimer = 0;
    if (manualClose.value || selectedSessionKey.value !== sessionKey) {
      return;
    }
    if (!timelineSnapshot.value?.isStreaming && !hasRunningRuntimeCards()) {
      return;
    }
    try {
      const thread = await fetchSessionThread(sessionKey);
      if (selectedSessionKey.value !== sessionKey) {
        return;
      }
      if (thread.status === "running" || thread.status === "cancel_requested") {
        return;
      }
      hydrateThread(thread);
      sending.value = false;
      cancelling.value = false;
      activeRunId.value = null;
      emit("refreshSessions");
    } catch {
      // 快照暂时拿不到就再等下一次 onerror。
    }
  }, STALE_STREAM_CHECK_MS);
}

function hasRunningRuntimeCards(events: UIRuntimeTimelineEvent[] = timelineSnapshot.value?.runtimeEvents ?? []): boolean {
  return events.some(
    (event) =>
      event.status === "running" ||
      event.status === "pending" ||
      event.status === "cancel_requested" ||
      hasRunningRuntimeCards(event.children),
  );
}

/** 重新拉取当前会话线程（run 结束或断流后的状态修复）。 */
async function reloadCurrentThread() {
  if (!selectedSessionKey.value) {
    return;
  }
  const thread = await fetchSessionThread(selectedSessionKey.value);
  hydrateThread(thread);
}

/** 根据会话摘要构造空线程，便于新建会话后立即切换界面。 */
function emptyThreadFromSummary(summary: SessionSummary): SessionThread {
  return {
    key: summary.key,
    title: summary.title,
    status: summary.status,
    messages: [],
    events: [],
    artifacts: [],
    has_pending_tool_calls: false,
    run_started_at: summary.run_started_at,
  };
}

// ---------------------------------------------------------------------------
// 卡片动作：精读 / 打开报告 / 抽屉追问
// ---------------------------------------------------------------------------

/** 卡片上的"精读"按钮：把精读请求作为一条普通消息发给主 Agent。 */
function requestDeepRead(paper: ChatPaperCard) {
  const already = paper.status === "deep_read" || readablePaperIds.value.has(paper.paper_id);
  const verb = already ? "请重新精读论文" : "请精读论文";
  submitMessage(`${verb} [${paper.paper_id}]《${paper.title}》`);
}

/** 卡片上的"报告"按钮：从工作区 REST 端点拉最新报告并打开抽屉。 */
async function openReportForPaper(paper: ChatPaperCard) {
  if (!selectedSessionKey.value) return;
  drawerVisible.value = true;
  drawerReport.value = null;
  try {
    const payload = await fetchPaperReport(selectedSessionKey.value, paper.paper_id);
    drawerReport.value = payload.report;
  } catch (error) {
    drawerVisible.value = false;
    handleError(error, "读取精读报告失败（可能尚未精读）");
  }
}

/** 对话流里的精读报告卡片：完整报告不在卡片里，向工作区再取一份再打开抽屉。 */
function openReportFromCard(payload: DeepReadCardPayload) {
  void openReportById(payload.paper_id);
}

/** 抽屉里的追问：把问题发回对话流（主 Agent 会调用 ask_paper 基于全文回答）。 */
function askFromDrawer(question: string) {
  const paperId = drawerReport.value?.paper_id ?? "";
  drawerVisible.value = false;
  submitMessage(`关于论文 [${paperId}]：${question}`);
}

function closeDrawer() {
  drawerVisible.value = false;
}

/** 面板里的「精读」按钮：按 paper_id 发起精读（不依赖卡片对象）。 */
function requestDeepReadById(paperId: string) {
  const already = readablePaperIds.value.has(paperId);
  submitMessage(already ? `请重新精读论文 ${paperId}` : `请精读论文 ${paperId}`);
}

/** 面板里的「报告」按钮：按 paper_id 打开精读报告抽屉。 */
async function openReportById(paperId: string) {
  if (!selectedSessionKey.value) return;
  drawerVisible.value = true;
  drawerReport.value = null;
  try {
    const payload = await fetchPaperReport(selectedSessionKey.value, paperId);
    drawerReport.value = payload.report;
  } catch (error) {
    drawerVisible.value = false;
    handleError(error, "读取精读报告失败（可能尚未精读）");
  }
}

/** 点击助手回复里的 [paper_id] 引用：已精读的直接开报告，否则弹一张论文信息卡片。

    中文注释：以前不管有没有精读都直接开报告抽屉，没精读的论文就只会弹一句失败提示，
    用户既看不到摘要也点不到原文。现在改成"有报告开报告、没报告出卡片"。
    卡片是独立浮层，不往对话流里插内容。
*/
function handlePaperClick(paperId: string) {
  if (readablePaperIds.value.has(paperId)) {
    void openReportById(paperId);
    return;
  }
  const paper = workspacePapers.value.get(paperId);
  if (!paper) {
    // 中文注释：正常走不到这里（引用按钮只在编号存在于工作区时才渲染），
    // 兜底沿用老行为打开报告，由它给出统一的失败提示。
    void openReportById(paperId);
    return;
  }
  dialogPaper.value = paper;
  paperDialogVisible.value = true;
}

/** 弹窗里的「精读」：关掉弹窗，把精读请求发回对话流。 */
function requestDeepReadFromDialog(paperId: string) {
  paperDialogVisible.value = false;
  dialogPaper.value = null;
  requestDeepReadById(paperId);
}

/** 关闭论文信息弹窗。 */
function closePaperDialog() {
  paperDialogVisible.value = false;
  dialogPaper.value = null;
}

function sameStringSet(current: Set<string>, next: readonly string[]) {
  if (current.size !== next.length) {
    return false;
  }
  return next.every((value) => current.has(value));
}

function statusTone(status: string) {
  if (status === "completed") return "success";
  if (status === "running") return "warning";
  if (status === "failed") return "danger";
  if (status === "interrupted") return "warning";
  return "neutral";
}

/** 统一把异常转成右上角 toast。 */
function handleError(error: unknown, title: string) {
  const description = error instanceof Error ? error.message : "未知错误";
  pushToast({ tone: "error", title, description });
}
</script>

<template>
  <section class="page-shell chat-view-shell">
    <header v-if="!showWelcome" class="chat-view-header">
      <!-- 中文说明：这个按钮只在窄屏出现（宽屏右栏一直在，不需要开关）。 -->
      <button
        type="button"
        class="panel-toggle-narrow"
        :aria-expanded="narrowPanelOpen"
        @click="narrowPanelOpen = !narrowPanelOpen"
      >
        <Library :size="15" />
        <span>论文</span>
      </button>
      <div class="chat-view-heading">
        <h1>{{ selectedTitle }}</h1>
        <p>多轮对话式论文调研：检索、筛选、评价、精读、追问与综述都由助手按需执行。</p>
      </div>
      <div class="chat-view-status">
        <LoaderCircle v-if="isRunning" class="spinning" :size="15" />
        <StatusPill :tone="statusTone(currentStatus)" :label="taskStatusLabel" />
      </div>
    </header>

    <div
      class="chat-main-layout"
      :style="layoutStyle"
      :data-panel="panelVisible ? 'true' : undefined"
      :data-narrow-open="narrowPanelOpen ? 'true' : undefined"
      :data-resizing="resizing"
    >
      <div class="chat-column">
        <!-- 中文说明：这条按钮放在滚动区外面，看最新回复时也能直接点。
             点完停在新加载的更早内容上，不用先滚回底部再往上抠。 -->
        <button
          v-if="hasOlderTurns && !threadLoading && !showWelcome"
          type="button"
          class="chat-load-older"
          @click="loadOlderTurns"
        >
          加载更早的对话（还有 {{ hiddenTurnCount }} 轮）
        </button>
        <div ref="scrollElement" class="chat-flow" :data-welcome="showWelcome" @scroll="handleScroll">
          <div v-if="showWelcome" class="chat-welcome">
            <ChatComposer
              v-model="draft"
              variant="welcome"
              heading="今天想调研什么方向？"
              helper-text="用一句话写下你想搞清楚的问题。助手会去检索论文、做成卡片，再陪你筛选、精读和追问。"
              placeholder="例如：帮我调研 LLM 推理优化的最新论文"
              :rows="2"
              :running="isRunning"
              :sending="sending || props.creatingSession"
              :cancellable="Boolean(activeRunId)"
              :cancelling="cancelling"
              :status-text="sending || isRunning || cancelling ? statusText : ''"
              @submit="submitMessage()"
              @cancel="cancelActiveRun"
            />
          </div>

          <template v-else>
            <div v-if="threadLoading" class="chat-loading">
              <LoaderCircle class="spinning" :size="16" />
              <span>正在恢复对话…</span>
            </div>

            <section v-for="(turn, turnIndex) in visibleFlowTurns" :key="turn.turnId ?? `turn-${turnIndex + hiddenTurnCount}`" class="chat-turn">
              <template v-for="message in turn.messages" :key="message.id">
                <UserBubble
                  v-if="message.role === 'user'"
                  :content="message.content"
                  :created-at="message.createdAt"
                  @resend="(content) => submitMessage(content)"
                />
                <AssistantBubble
                  v-else-if="message.role === 'assistant'"
                  :content="message.content"
                  :reasoning="message.reasoning"
                  :is-streaming="message.isStreaming"
                  :reasoning-streaming="message.reasoningStreaming"
                  :known-paper-ids="workspacePaperIds"
                  @paper-click="handlePaperClick"
                />
                <PaperCardGroup
                  v-else-if="message.kind === 'paper_list' && asPaperList(message)"
                  :payload="asPaperList(message)!"
                  :readable-paper-ids="readablePaperIds"
                  :busy="isRunning || sending"
                  @deep-read="requestDeepRead"
                  @open-report="openReportForPaper"
                />
                <DeepReadReportCard
                  v-else-if="message.kind === 'deep_read_report' && asDeepRead(message)"
                  :payload="asDeepRead(message)!"
                  @open="openReportFromCard"
                />
                <ReviewMessage
                  v-else-if="message.kind === 'review' && asReview(message)"
                  :payload="asReview(message)!"
                  :session-key="selectedSessionKey"
                />
                <div v-else-if="message.kind === 'error'" class="chat-system-line" data-tone="danger">
                  <MathText :text="message.content" />
                  <button
                    type="button"
                    class="chat-retry-button"
                    @click="retryTurn(turn.turnId)"
                  >
                    重试
                  </button>
                </div>
                <div v-else-if="message.role === 'system' && message.content" class="chat-system-line">
                  <MathText :text="message.content" />
                </div>
              </template>

              <AssistantBubble
                v-if="turnNeedsStreamingPlaceholder(turn, turnIndex === visibleFlowTurns.length - 1)"
                content=""
                :is-streaming="true"
              />

              <ToolCallTrace
                :events="turn.events"
                :active="isRunning && turnIndex === visibleFlowTurns.length - 1"
                :on-resume="resumeHandler"
              />
            </section>
          </template>
        </div>

        <ChatComposer
          v-if="!showWelcome"
          v-model="draft"
          :running="isRunning"
          :sending="sending || props.creatingSession"
          :cancellable="Boolean(activeRunId)"
          :cancelling="cancelling"
          :status-text="statusText"
          @submit="submitMessage()"
          @cancel="cancelActiveRun"
        />
      </div>

      <!-- 回到底部按钮：用户向上滚动后出现 -->
      <button
        v-if="showScrollToBottom"
        type="button"
        class="scroll-to-bottom-button"
        @click="scrollToBottomManual"
        title="回到底部"
      >
        ↓
      </button>

      <!-- 中文说明：右栏的拖拽把手，只有右栏真的显示出来时才出现。
           按住左右拖动可以调整论文工作区的宽度，双击恢复成默认的 320px。 -->
      <button
        v-if="selectedSessionKey && !showWelcome"
        type="button"
        class="panel-resizer"
        aria-label="拖拽调整论文工作区宽度"
        title="左右拖动调整宽度，双击恢复默认"
        @pointerdown.prevent="startPanelResize"
        @dblclick="resetPanelWidth"
      ></button>

      <PaperLibraryPanel
        v-if="selectedSessionKey && !showWelcome"
        ref="libraryPanelRef"
        :session-key="selectedSessionKey"
        :busy="isRunning || sending"
        @request-deep-read="(paperId) => requestDeepReadById(paperId)"
        @open-report="openReportById"
        @workspace-changed="refreshWorkspacePapers"
      />
    </div>


    <DeepReadDrawer
      :visible="drawerVisible"
      :report="drawerReport"
      :session-key="selectedSessionKey"
      :busy="isRunning || sending"
      @close="closeDrawer"
      @ask="askFromDrawer"
    />

    <!-- 点击引用但该论文尚未精读时，用弹窗展示标题、摘要和原文链接 -->
    <PaperInfoDialog
      :visible="paperDialogVisible"
      :paper="dialogPaper"
      :busy="isRunning || sending"
      @close="closePaperDialog"
      @deep-read="requestDeepReadFromDialog"
    />
  </section>
</template>
