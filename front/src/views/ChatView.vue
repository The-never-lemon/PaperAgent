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
 * run 生命周期（提交 → runs → SSE → 聚合 → turn_end 收尾 → 刷新恢复）与
 * 取消/竞态保护完全复用原工作台的成熟模式，聚合逻辑在 SessionStreamAggregator。
 * props/emits 契约与原 SessionWorkspaceView 保持一致，App.vue 无需改动。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { LoaderCircle } from "lucide-vue-next";

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
const timelineSnapshot = ref<SessionTimelineSnapshot | null>(null);
const scrollElement = ref<HTMLElement | null>(null);
// 中文注释：用户是否手动向上滚动过（离开底部）。流式输出时如果用户向上滚了，
// 就不自动滚到底部，直到用户点击「回到底部」按钮。
const userScrolledUp = ref(false);
// 中文注释：是否显示「回到底部」按钮。
const showScrollToBottom = ref(false);
// 中文注释：SSE 断线重连的尝试次数。每次成功重连后重置为 0。
// 指数退避：1s / 2s / 4s / 8s / 16s，最多 5 次。
const reconnectAttempts = ref(0);
const MAX_RECONNECT_ATTEMPTS = 5;

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
const currentStatus = computed(() => timelineSnapshot.value?.status ?? selectedSummary.value?.status ?? "created");
const isRunning = computed(() => timelineSnapshot.value?.isStreaming ?? false);
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

/** 把聚合器快照写回响应式状态。只有在用户没有手动向上滚动时才自动滚到底部。 */
function syncSnapshot() {
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
    readablePaperIds.value = new Set(
      snapshot.papers.filter((paper) => paper.has_report).map((paper) => paper.paper_id),
    );
    workspacePaperIds.value = new Set(snapshot.papers.map((paper) => paper.paper_id));
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
      aggregator.apply(event);
      syncSnapshot();
      // 中文注释：检索工具一执行完就会推一条 kind=paper_list 的消息，这时论文已经写进工作区。
      // 顺手让左侧论文面板重拉一次，用户不用刷新页面就能看到刚检索到的论文。
      const cardKind = typeof event.metadata?.kind === "string" ? event.metadata.kind : "";
      if (event.event === "message" && cardKind === "paper_list") {
        void libraryPanelRef.value?.refresh();
      }
      if (event.event === "turn_end") {
        await handleRunFinished(sessionKey, event);
      }
    },
    onError: async () => {
      if (manualClose.value) {
        return;
      }
      // 中文注释：断线后指数退避重连。每次 onError 都会触发，但浏览器 EventSource
      // 自己也会尝试重连（默认 3s）。这里在浏览器重连失败后再做指数退避，
      // 避免和浏览器自己的重连机制打架。
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

/** run 结束：刷新线程与工作区，让落库后的消息、卡片和产物状态同步到界面。 */
async function handleRunFinished(sessionKey: string, event: SessionRuntimeEvent) {
  closeStream(true);
  sending.value = false;
  cancelling.value = false;
  activeRunId.value = null;
  await reloadCurrentThread();
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
  if (streamSource.value) {
    streamSource.value.close();
    streamSource.value = null;
  }
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
  submitMessage(`请精读论文 [${paper.paper_id}]《${paper.title}》`);
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

/** 对话流里的精读报告卡片：直接用卡片携带的报告打开抽屉。 */
function openReportFromCard(payload: DeepReadCardPayload) {
  drawerReport.value = payload.report;
  drawerVisible.value = true;
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
  submitMessage(`请精读论文 ${paperId}`);
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
      <div class="chat-view-heading">
        <h1>{{ selectedTitle }}</h1>
        <p>多轮对话式论文调研：检索、筛选、评价、精读、追问与综述都由助手按需执行。</p>
      </div>
      <div class="chat-view-status">
        <LoaderCircle v-if="isRunning" class="spinning" :size="15" />
        <StatusPill :tone="statusTone(currentStatus)" :label="taskStatusLabel" />
      </div>
    </header>

    <div class="chat-main-layout">
      <div ref="scrollElement" class="chat-flow" :data-welcome="showWelcome" @scroll="handleScroll">
        <div v-if="showWelcome" class="chat-welcome">
          <ChatComposer
            v-model="draft"
            variant="welcome"
            heading="今天想调研什么方向？"
            helper-text="直接用一句话描述你的调研需求，助手会检索论文、给出卡片，并陪你逐步筛选、精读和追问。"
            placeholder="例如：帮我调研 LLM 推理优化的最新论文"
            :rows="3"
            :running="isRunning"
            :sending="sending || props.creatingSession"
            :cancellable="Boolean(activeRunId)"
            :cancelling="cancelling"
            :status-text="statusText"
            @submit="submitMessage()"
            @cancel="cancelActiveRun"
          />
        </div>

        <template v-else>
          <div v-if="threadLoading" class="chat-loading">
            <LoaderCircle class="spinning" :size="16" />
            <span>正在恢复对话…</span>
          </div>

          <section v-for="(turn, turnIndex) in flowTurns" :key="turn.turnId ?? `turn-${turnIndex}`" class="chat-turn">
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

            <ToolCallTrace
              :events="turn.events"
              :active="isRunning && turnIndex === flowTurns.length - 1"
              :on-resume="resumeHandler"
            />
          </section>
        </template>
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
