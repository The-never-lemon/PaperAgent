import type { ChatCardKind, ChatCardPayload, DeepReadCardPayload } from "../types/chat";
import type {
  RuntimeDetailContent,
  SessionArtifact,
  SessionRuntimeEvent,
  SessionThread,
  SessionTimelineSnapshot,
  StoredSessionEvent,
  UIRuntimeTimelineEvent,
  UISessionMessage,
} from "../types/sessions";
import { createRandomId } from "./random-id";

/** 中文注释：后端冻结协议里 metadata.kind 的三种卡片值（见 types/chat.ts）。 */
const CARD_KINDS: readonly ChatCardKind[] = ["paper_list", "deep_read_report", "review"];

function isCardKind(value: string): value is ChatCardKind {
  return (CARD_KINDS as readonly string[]).includes(value);
}

/** 用户气泡的稳定 id：乐观插入、SSE 回放、hydrate 必须用同一个，Vue 才不会拆掉重挂。 */
export function stableUserMessageId(turnId: string | null | undefined): string {
  return `user:${turnId || "none"}`;
}

function createMessage(partial: Partial<UISessionMessage>): UISessionMessage {
  return {
    id: partial.id ?? createRandomId("message"),
    role: partial.role ?? "assistant",
    kind: partial.kind ?? "message",
    content: partial.content ?? "",
    reasoning: partial.reasoning ?? "",
    isStreaming: partial.isStreaming ?? false,
    reasoningStreaming: partial.reasoningStreaming ?? false,
    media: partial.media ?? [],
    toolEvents: partial.toolEvents ?? [],
    artifactRefs: partial.artifactRefs ?? [],
    turnId: partial.turnId ?? null,
    createdAt: partial.createdAt ?? null,
    card: partial.card ?? null,
  };
}

function createRuntimeEvent(partial: Partial<UIRuntimeTimelineEvent> & { id: string }): UIRuntimeTimelineEvent {
  return {
    id: partial.id,
    parentId: partial.parentId ?? null,
    type: partial.type ?? "runtime_event",
    title: partial.title ?? "执行事件",
    status: partial.status ?? "running",
    showContent: partial.showContent ?? "正在处理",
    detailContent: partial.detailContent ?? null,
    metadata: partial.metadata ?? {},
    createdAt: partial.createdAt ?? null,
    updatedAt: partial.updatedAt ?? null,
    completedAt: partial.completedAt ?? null,
    children: partial.children ?? [],
    isCollapsed: partial.isCollapsed ?? false,
    completed: partial.completed ?? null,
    total: partial.total ?? null,
    inputTokens: partial.inputTokens ?? 0,
    outputTokens: partial.outputTokens ?? 0,
    raw: partial.raw ?? ({ event: "runtime_event", session_key: "" } as SessionRuntimeEvent),
  };
}

const FINISHED_STATUSES = new Set(["completed", "failed", "cancelled", "skipped"]);
const TERMINAL_SESSION_STATUSES = new Set(["completed", "failed", "cancelled", "interrupted"]);

function isTerminalSessionStatus(status: string | null | undefined): boolean {
  return Boolean(status && TERMINAL_SESSION_STATUSES.has(status));
}

export class SessionStreamAggregator {
  private messages: UISessionMessage[] = [];
  private runtimeEvents: UIRuntimeTimelineEvent[] = [];
  private runtimeEventMap = new Map<string, UIRuntimeTimelineEvent>();
  private artifacts: SessionArtifact[] = [];
  private isStreaming = false;
  private runStartedAt: string | null = null;
  private streamError: SessionRuntimeEvent | null = null;
  private status = "created";
  private activeAssistantId: string | null = null;
  private activeNodeKey: string | null = null;
  // 中文注释：已经应用过的最大 stream_seq。SSE 重连会把内存里的历史再推一遍，
  // 靠它跳过旧事件，避免正文被拼两遍、整页跟着刷一次。
  private lastStreamSeq = 0;
  // 中文注释：重放历史事件时先不要每条都把整棵执行树加一遍 token，
  // 等全部事件走完再加一次。打开很长的旧会话时能少做很多重复计算。
  private skipTokenTotals = false;

  /** 用线程快照重建时间线状态，保证历史回放和实时展示走同一条路。 */
  hydrate(thread: SessionThread) {
    this.messages = [];
    // 中文注释：执行过程现在统一放在 runtimeEvents 里，前端只按 id 更新事件，不再追加旧 node 行。
    this.runtimeEvents = [];
    this.runtimeEventMap = new Map();
    this.artifacts = [...thread.artifacts];
    this.isStreaming = thread.status === "running" || thread.status === "cancel_requested" || thread.has_pending_tool_calls;
    this.runStartedAt = thread.run_started_at;
    this.streamError = null;
    this.status = thread.status;
    this.activeAssistantId = null;
    this.activeNodeKey = null;
    this.lastStreamSeq = 0;

    if (thread.events.length > 0) {
      this.skipTokenTotals = true;
      try {
        for (const storedEvent of thread.events) {
          this.apply(this.normalizeStoredEvent(storedEvent));
        }
      } finally {
        this.skipTokenTotals = false;
        this.recalculateTokenTotals();
      }
    }

    this.reconcileTerminalThread(thread);

    // 中文注释：兼容没有事件流的老数据，如果历史里没有消息事件，就从消息表重建基础聊天内容。
    if (this.messages.length === 0) {
      for (const message of thread.messages) {
        // 中文注释：工具结果行（role=tool）和带 tool_calls 的助手中间行属于执行痕迹，
        // 聊天界面靠运行事件卡片展示它们，兜底重建时跳过，避免出现一堆原始 JSON 气泡。
        if (message.role === "tool") {
          continue;
        }
        if (message.role === "assistant" && Array.isArray(message.tool_calls) && message.tool_calls.length > 0) {
          continue;
        }
        if (message.role !== "user" && message.role !== "assistant" && message.role !== "system") {
          continue;
        }
        this.messages.push(
          createMessage({
            id: message.id,
            role: message.role,
            kind: message.kind ?? "message",
            content: message.content,
            reasoning: message.reasoning ?? "",
            media: message.media ?? [],
            turnId: message.turn_id ?? null,
            createdAt: message.created_at,
          }),
        );
      }
    }
  }

  /** 乐观插入一条用户消息，减少提交时的等待感。 */
  addOptimisticUserMessage(content: string, turnId: string) {
    this.messages.push(
      createMessage({
        id: stableUserMessageId(turnId),
        role: "user",
        content,
        turnId,
        createdAt: new Date().toISOString(),
      }),
    );
    this.isStreaming = true;
    this.status = "running";
  }

  /**
   * 应用一条运行事件。
   * 返回 false 表示这条事件没有造成可见变化（已应用过的重放、重复的用户回声），
   * 调用方就不该整页刷快照。
   */
  apply(event: SessionRuntimeEvent): boolean {
    if (this.shouldSkipDuplicateSeq(event)) {
      return false;
    }
    switch (event.event) {
      case "runtime_event":
        return this.applyRuntimeEvent(event);
      case "message":
        return this.applyMessageEvent(event);
      case "delta":
        this.ensureActiveAssistant(event).content += event.content ?? event.delta ?? "";
        this.ensureAssistantStreaming(event);
        return true;
      case "reasoning_delta":
        this.ensureActiveAssistant(event).reasoning += event.content ?? event.delta ?? "";
        this.ensureActiveAssistant(event).reasoningStreaming = true;
        this.ensureAssistantStreaming(event);
        return true;
      case "reasoning_end":
        this.ensureActiveAssistant(event).reasoningStreaming = false;
        return true;
      case "artifact":
        this.applyArtifactEvent(event);
        return true;
      case "status":
        this.status = event.status ?? this.status;
        if ("run_started_at" in event) {
          this.runStartedAt = event.run_started_at ?? null;
        }
        this.isStreaming = event.status === "running" || event.status === "cancel_requested";
        // 中文注释：只有正在运行或正在停止时保留忙碌状态，其他状态都要结束“正在生成中”。
        if (event.status && event.status !== "running" && event.status !== "cancel_requested") {
          this.runStartedAt = null;
          this.stopActiveAssistantStreaming();
          this.activeNodeKey = null;
        }
        return true;
      case "error":
        this.streamError = event;
        this.messages.push(
          createMessage({
            id: this.messageIdFromEvent(event, "error"),
            role: "system",
            kind: "error",
            content: event.message ?? event.content ?? "运行失败",
            turnId: event.turn_id ?? null,
            createdAt: event.timestamp ?? new Date().toISOString(),
          }),
        );
        this.status = "failed";
        this.isStreaming = false;
        this.activeNodeKey = null;
        this.stopActiveAssistantStreaming();
        return true;
      case "turn_end":
        this.status = event.status ?? this.status;
        this.settleUnfinishedRuntimeEvents(this.status, event);
        this.isStreaming = false;
        this.activeNodeKey = null;
        this.stopActiveAssistantStreaming();
        return true;
      default:
        return false;
    }
  }

  /** 已经应用过的 stream_seq 再来一次就丢掉，避免 SSE 重连把历史再吃一遍。 */
  private shouldSkipDuplicateSeq(event: SessionRuntimeEvent): boolean {
    const seq = event.stream_seq;
    if (typeof seq !== "number" || !Number.isFinite(seq) || seq <= 0) {
      return false;
    }
    // 中文注释：落库事件用数据库 seq_no，流式 token 用当前 run 内存队列长度，
    // 两套序号会交错变大变小。思考增量一旦把水位抬到 1200，后面 seq=242 的
    // 「工具完成」和 turn_end 若也按「更小就丢」处理，卡片就会永远停在处理中。
    // 只对 token 增量去重；工具卡片、正文和结束事件按 id 更新，重放也安全。
    const isTokenDelta = event.event === "delta" || event.event === "reasoning_delta";
    if (isTokenDelta && seq <= this.lastStreamSeq) {
      return true;
    }
    if (seq > this.lastStreamSeq) {
      this.lastStreamSeq = seq;
    }
    return false;
  }

  /** 返回当前时间线快照。只浅拷贝顶层数组，不再把执行树整棵复制一遍。 */
  snapshot(): SessionTimelineSnapshot {
    return {
      messages: this.messages.slice(),
      runtimeEvents: this.runtimeEvents.slice(),
      activeNodeKey: this.activeNodeKey,
      artifacts: this.artifacts.slice(),
      isStreaming: this.isStreaming,
      runStartedAt: this.runStartedAt,
      streamError: this.streamError,
      status: this.status,
    };
  }

  /** 把历史事件表里的记录转成和 SSE 对齐的统一结构。 */
  private normalizeStoredEvent(event: StoredSessionEvent): SessionRuntimeEvent {
    const metadata = event.metadata ?? {};
    if (event.event_type === "status_change") {
      const normalized: SessionRuntimeEvent = {
        event: "status",
        session_key: String(metadata.session_key ?? ""),
        status: String(metadata.status ?? event.content ?? "created"),
        turn_id: typeof metadata.turn_id === "string" ? metadata.turn_id : undefined,
        timestamp: String(metadata.timestamp ?? event.created_at),
      };
      if ("run_started_at" in metadata) {
        normalized.run_started_at = (metadata.run_started_at as string | null | undefined) ?? null;
      }
      return normalized;
    }

    return {
      ...(metadata as SessionRuntimeEvent),
      event: event.event_type,
      session_key: String(metadata.session_key ?? ""),
      content: String(metadata.content ?? metadata.message ?? event.content ?? ""),
      timestamp: String(metadata.timestamp ?? event.created_at),
      stream_seq: Number(metadata.stream_seq ?? event.seq_no),
    };
  }

  /** 处理普通 message 事件，包括用户消息、助手消息、对话卡片和旧版 progress 卡片。 */
  private applyMessageEvent(event: SessionRuntimeEvent): boolean {
    const kind = event.kind ?? "message";
    const role = event.role ?? "assistant";
    const metadata = (event.metadata ?? {}) as Record<string, unknown>;
    const contentKind = typeof metadata.kind === "string" ? metadata.kind : "";

    // 中文注释：对话式调研的卡片协议——metadata.kind 是 paper_list / deep_read_report /
    // review 时，把整份 metadata 作为卡片载荷存进一条独立的 system 消息，
    // 聊天界面按 kind 渲染对应卡片组件。卡片消息绝不能走助手分支，
    // 否则卡片摘要文字会覆盖正在流式输出的助手气泡正文。
    if (isCardKind(contentKind)) {
      this.messages.push(
        createMessage({
          id: this.messageIdFromEvent(event, contentKind),
          role: "system",
          kind: contentKind,
          content: event.content ?? "",
          card: slimCardPayload(contentKind, metadata),
          turnId: event.turn_id ?? null,
          createdAt: event.timestamp ?? new Date().toISOString(),
        }),
      );
      this.isStreaming = true;
      this.status = "running";
      return true;
    }

    if (kind === "progress" || kind === "tool" || kind === "tool_hint") {
      this.messages.push(
        createMessage({
          id: this.messageIdFromEvent(event, kind),
          role: "system",
          kind,
          content: event.content ?? "",
          turnId: event.turn_id ?? null,
          createdAt: event.timestamp ?? new Date().toISOString(),
        }),
      );
      this.isStreaming = true;
      this.status = "running";
      return true;
    }

    if (role === "user") {
      const turnId = event.turn_id ?? null;
      const existing = this.messages.find(
        (message) =>
          message.role === "user"
          && (message.id === stableUserMessageId(turnId)
            || (message.turnId === turnId && message.content === (event.content ?? ""))),
      );
      if (existing) {
        // 中文注释：提交时已经乐观插入过这条用户消息。后端回放只是确认，
        // 不要换 id、也不要通知界面整段重绘。
        existing.createdAt = event.timestamp ?? existing.createdAt;
        if (event.media?.length) {
          existing.media = event.media;
        }
        return false;
      }
      this.messages.push(
        createMessage({
          id: stableUserMessageId(turnId),
          role: "user",
          content: event.content ?? "",
          media: event.media ?? [],
          turnId,
          createdAt: event.timestamp ?? new Date().toISOString(),
        }),
      );
      return true;
    }

    const assistant = this.ensureActiveAssistant(event);
    assistant.content = event.content ?? assistant.content;
    assistant.media = [...assistant.media, ...(event.media ?? [])];
    assistant.createdAt = event.timestamp ?? assistant.createdAt;
    assistant.isStreaming = true;
    this.isStreaming = true;
    this.status = "running";

    // 中文注释：对话协议里每一轮助手正文都以 metadata.kind=="text" 的 message 事件收尾。
    // 收到它就把当前气泡"定格"（清空 activeAssistantId）：这一轮的文字不再接受新的
    // 增量，下一轮的 delta/reasoning 会开一张新气泡。这样多轮工具调用之间，
    // 气泡和卡片消息的时间顺序才是正确的。
    if (contentKind === "text") {
      assistant.isStreaming = false;
      this.activeAssistantId = null;
    }
    return true;
  }

  /** 按 runtime_event.id 更新执行过程；同一个 id 永远只显示一个事件。 */
  private applyRuntimeEvent(event: SessionRuntimeEvent): boolean {
    const eventId = String(event.id ?? "").trim();
    if (!eventId) {
      return false;
    }

    const item = this.ensureRuntimeEvent(eventId);
    this.updateRuntimeEvent(item, event);

    if (item.parentId) {
      const parent = this.ensureRuntimeEvent(item.parentId);
      this.attachChild(parent, item);
    } else {
      this.attachRoot(item);
    }
    // 每次子卡片变化后都重新汇总，父卡片始终等于所有子卡片之和。
    // 重放历史时跳过，等全部事件走完再加一次。
    if (!this.skipTokenTotals) {
      this.recalculateTokenTotals();
    }

    if (item.status === "failed") {
      this.status = "failed";
      this.isStreaming = false;
      this.activeNodeKey = null;
      this.stopActiveAssistantStreaming();
      return true;
    }

    if (item.status === "running") {
      this.status = "running";
      this.isStreaming = true;
      this.activeNodeKey = this.nodeKeyFromRuntimeEvent(item);
      return true;
    }

    if (item.status === "completed") {
      // 中文注释：某个事件完成不代表整个工作流完成，所以这里只更新事件本身，最终状态仍等 turn_end。
      this.status = this.status === "created" ? "running" : this.status;
    }
    return true;
  }

  /** 找到或创建一个执行事件，子事件先到时也能先占位。 */
  private ensureRuntimeEvent(id: string): UIRuntimeTimelineEvent {
    const existing = this.runtimeEventMap.get(id);
    if (existing) {
      return existing;
    }
    const item = createRuntimeEvent({
      id,
      title: "执行事件",
      showContent: "等待事件详情",
      raw: { event: "runtime_event", session_key: "" },
    });
    this.runtimeEventMap.set(id, item);
    return item;
  }

  /** 用后端新事件覆盖旧显示内容，metadata 则保留旧字段并用新字段覆盖。 */
  private updateRuntimeEvent(item: UIRuntimeTimelineEvent, event: SessionRuntimeEvent) {
    const metadata = event.metadata ?? {};
    item.parentId = typeof event.parent_id === "string" && event.parent_id ? event.parent_id : null;
    item.type = event.type ?? item.type;
    item.title = event.title ?? item.title;
    item.status = event.status ?? item.status;
    item.showContent = event.show_content ?? event.message ?? event.content ?? item.showContent;
    item.detailContent = normalizeDetailContent(event.detail_content ?? item.detailContent);
    item.metadata = { ...item.metadata, ...metadata };
    item.createdAt = event.created_at ?? event.timestamp ?? item.createdAt;
    item.updatedAt = event.updated_at ?? event.timestamp ?? item.updatedAt;
    item.completedAt = event.completed_at ?? (FINISHED_STATUSES.has(item.status) ? item.updatedAt : item.completedAt);
    // 中文注释：折叠状态交给 RuntimeEventTree 自己记。这里如果按 completed 强行改，
    // 每个工具一结束父节点就会合上再打开，整条执行链看起来像被强制刷新。
    item.completed = typeof metadata.completed === "number" ? metadata.completed : item.completed;
    item.total = typeof metadata.total === "number" ? metadata.total : item.total;
    if (typeof event.input_tokens === "number") {
      item.inputTokens = Math.max(0, event.input_tokens);
    } else if (typeof metadata.input_tokens === "number") {
      item.inputTokens = Math.max(0, metadata.input_tokens);
    }
    if (typeof event.output_tokens === "number") {
      item.outputTokens = Math.max(0, event.output_tokens);
    } else if (typeof metadata.output_tokens === "number") {
      item.outputTokens = Math.max(0, metadata.output_tokens);
    }
    item.raw = event;
  }

  /** 叶子卡片使用模型返回值，父卡片只显示当前所有子卡片的合计。 */
  private recalculateTokenTotals() {
    const update = (event: UIRuntimeTimelineEvent): { input: number; output: number } => {
      if (event.children.length === 0) {
        return { input: event.inputTokens, output: event.outputTokens };
      }
      const total = event.children.reduce(
        (sum, child) => {
          const childTotal = update(child);
          return {
            input: sum.input + childTotal.input,
            output: sum.output + childTotal.output,
          };
        },
        { input: 0, output: 0 },
      );
      event.inputTokens = total.input;
      event.outputTokens = total.output;
      return total;
    };

    for (const event of this.runtimeEvents) {
      update(event);
    }
  }

  /**
   * 线程快照已经是终态时，以会话状态为准收尾。
   * 实时流若漏掉工具完成事件，hydrate 后仍可能留下「处理中」卡片。
   */
  private reconcileTerminalThread(thread: SessionThread) {
    if (!isTerminalSessionStatus(thread.status)) {
      return;
    }
    this.status = thread.status;
    this.isStreaming = false;
    this.runStartedAt = null;
    this.activeNodeKey = null;
    this.stopActiveAssistantStreaming();
    this.settleUnfinishedRuntimeEvents(thread.status, {
      event: "turn_end",
      session_key: thread.key,
      status: thread.status,
      timestamp: new Date().toISOString(),
    });
  }

  /** 运行已经结束时，把还停在处理中的卡片收成与会话一致的终态。 */
  private settleUnfinishedRuntimeEvents(status: string, event: SessionRuntimeEvent) {
    if (status === "cancelled") {
      this.markUnfinishedRuntimeEventsCancelled(event);
      return;
    }
    if (!isTerminalSessionStatus(status)) {
      return;
    }
    this.markUnfinishedRuntimeEventsCompleted(event, status === "failed" ? "failed" : "completed");
  }

  /**
   * 将运行树中还没有结束的节点统一标记为已取消。
   * 已完成、已失败或已经跳过的节点不改动，保证用户仍能看到中断前已经完成的结果。
   */
  private markUnfinishedRuntimeEventsCancelled(event: SessionRuntimeEvent) {
    const timestamp = event.timestamp ?? new Date().toISOString();
    const update = (item: UIRuntimeTimelineEvent) => {
      if (item.status === "running" || item.status === "pending" || item.status === "cancel_requested") {
        item.status = "cancelled";
        item.showContent = "已停止";
        item.updatedAt = timestamp;
        item.completedAt = timestamp;
        item.metadata = {
          ...item.metadata,
          cancellation_reason: "user_requested",
        };
        item.raw = {
          ...item.raw,
          event: "runtime_event",
          status: "cancelled",
          message: "任务已按用户请求停止",
          timestamp,
        };
      }

      for (const child of item.children) {
        update(child);
      }
    };

    for (const item of this.runtimeEvents) {
      update(item);
    }
    this.recalculateTokenTotals();
  }

  /** 运行成功或失败结束后，把漏掉完成事件的处理中卡片收成终态。 */
  private markUnfinishedRuntimeEventsCompleted(event: SessionRuntimeEvent, status: "completed" | "failed") {
    const timestamp = event.timestamp ?? new Date().toISOString();
    const showContent = status === "failed" ? "执行失败" : "已完成";
    const update = (item: UIRuntimeTimelineEvent) => {
      if (item.status === "running" || item.status === "pending" || item.status === "cancel_requested") {
        item.status = status;
        item.showContent = showContent;
        item.updatedAt = timestamp;
        item.completedAt = timestamp;
        item.raw = {
          ...item.raw,
          event: "runtime_event",
          status,
          message: showContent,
          timestamp,
        };
      }
      for (const child of item.children) {
        update(child);
      }
    };

    for (const item of this.runtimeEvents) {
      update(item);
    }
    this.recalculateTokenTotals();
  }

  /** 把子事件挂到父事件下面；如果已经挂过，就只保持原位置，不重复插入。 */
  private attachChild(parent: UIRuntimeTimelineEvent, child: UIRuntimeTimelineEvent) {
    this.runtimeEvents = this.runtimeEvents.filter((event) => event.id !== child.id);
    if (!parent.children.some((event) => event.id === child.id)) {
      parent.children.push(child);
    }
    this.attachRoot(parent);
  }

  /** 把没有父级的事件放到根列表。 */
  private attachRoot(item: UIRuntimeTimelineEvent) {
    if (!this.runtimeEvents.some((event) => event.id === item.id)) {
      this.runtimeEvents.push(item);
    }
  }

  /** 处理 artifact 事件，并把产物挂到当前助手消息下面。 */
  private applyArtifactEvent(event: SessionRuntimeEvent) {
    const artifact = event.artifact;
    if (!artifact || typeof artifact !== "object") {
      return;
    }
    const artifactId = String((artifact as Record<string, unknown>).id ?? (artifact as Record<string, unknown>).artifact_id ?? "");
    if (artifactId && !this.artifacts.some((item) => item.id === artifactId)) {
      this.artifacts.push({
        id: artifactId,
        artifact_type: String((artifact as Record<string, unknown>).artifact_type ?? "artifact"),
        name: String((artifact as Record<string, unknown>).name ?? "artifact"),
        path: String((artifact as Record<string, unknown>).path ?? ""),
        size: Number((artifact as Record<string, unknown>).size ?? 0),
        created_at: String((artifact as Record<string, unknown>).created_at ?? event.timestamp ?? new Date().toISOString()),
        metadata: ((artifact as Record<string, unknown>).metadata as Record<string, unknown> | undefined) ?? {},
      });
    }
    this.ensureActiveAssistant(event).artifactRefs.push(artifact);
  }

  /** 确保当前存在一张可以承接流式输出的 assistant 卡片。 */
  private ensureActiveAssistant(event: SessionRuntimeEvent): UISessionMessage {
    const existing = this.activeAssistant();
    if (existing) {
      return existing;
    }
    const message = createMessage({
      id: this.messageIdFromEvent(event, "assistant"),
      role: "assistant",
      isStreaming: true,
      turnId: event.turn_id ?? null,
      createdAt: event.timestamp ?? new Date().toISOString(),
    });
    this.messages.push(message);
    this.activeAssistantId = message.id;
    return message;
  }

  /** 消息在实时流和 hydrate 回放里必须拿到同一个 id，否则 Vue 会按新 key 拆掉旧气泡。 */
  private messageIdFromEvent(event: SessionRuntimeEvent, kind: string): string {
    const turnId = event.turn_id || "none";
    const seq = typeof event.stream_seq === "number" && event.stream_seq > 0
      ? event.stream_seq
      : event.event_id || "pending";
    return `msg:${turnId}:${kind}:${seq}`;
  }

  /** 把当前 assistant 标记成正在流式输出。 */
  private ensureAssistantStreaming(event: SessionRuntimeEvent) {
    const assistant = this.ensureActiveAssistant(event);
    assistant.isStreaming = true;
    this.isStreaming = true;
    this.status = "running";
  }

  /** 失败或结束时，同时关掉整体状态和单条消息状态，避免界面残留“正在生成中”。 */
  private stopActiveAssistantStreaming() {
    const assistant = this.activeAssistant();
    if (assistant) {
      assistant.isStreaming = false;
      assistant.reasoningStreaming = false;
    }
    this.activeAssistantId = null;
  }

  /** 取出当前正在拼接中的 assistant 消息。 */
  private activeAssistant(): UISessionMessage | undefined {
    if (!this.activeAssistantId) {
      return undefined;
    }
    return this.messages.find((message) => message.id === this.activeAssistantId);
  }

  /** 从 runtime_event 的 metadata 里取节点 key，供顶部运行状态做轻量提示。 */
  private nodeKeyFromRuntimeEvent(item: UIRuntimeTimelineEvent) {
    const nodeKey = item.metadata.node_key;
    return typeof nodeKey === "string" ? nodeKey : null;
  }
}

/** 把卡片事件收成界面真正要用的那一小份数据。精读卡片丢掉整份报告。 */
function slimCardPayload(kind: ChatCardKind, metadata: Record<string, unknown>): ChatCardPayload {
  if (kind === "deep_read_report") {
    return slimDeepReadCard(metadata);
  }
  return metadata as ChatCardPayload;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function scoreFrom(value: unknown): number {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  const record = asRecord(value);
  if (typeof record.score === "number" && Number.isFinite(record.score)) {
    return record.score;
  }
  return 0;
}

/** 精读卡片只留下标题、总结和四个分数，完整报告不进聊天快照。 */
function slimDeepReadCard(metadata: Record<string, unknown>): DeepReadCardPayload {
  const report = asRecord(metadata.report);
  return {
    kind: "deep_read_report",
    paper_id: String(metadata.paper_id ?? report.paper_id ?? ""),
    source: String(metadata.source ?? report.source ?? ""),
    artifact_id: String(metadata.artifact_id ?? report.artifact_id ?? ""),
    fulltext_available: Boolean(metadata.fulltext_available),
    fulltext_failure_reason: String(metadata.fulltext_failure_reason ?? ""),
    title: String(metadata.title ?? report.title ?? ""),
    short_summary: String(metadata.short_summary ?? report.short_summary ?? ""),
    overall_score: scoreFrom(metadata.overall_score ?? report.overall_score),
    relevance: scoreFrom(metadata.relevance ?? report.relevance),
    novelty: scoreFrom(metadata.novelty ?? report.novelty),
    rigor: scoreFrom(metadata.rigor ?? report.rigor),
    clarity: scoreFrom(metadata.clarity ?? report.clarity),
  };
}

function normalizeDetailContent(value: RuntimeDetailContent | undefined): RuntimeDetailContent {
  if (value === undefined) {
    return null;
  }
  if (value === null || typeof value === "string") {
    return value;
  }
  return { ...value };
}
