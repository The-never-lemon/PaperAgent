import type { ChatCardPayload } from "./chat";

export type SessionStatus =
  | "created"
  | "running"
  | "cancel_requested"
  | "cancelled"
  | "completed"
  | "failed"
  | "interrupted";

export interface SessionSummary {
  key: string;
  title: string;
  preview: string;
  updated_at: string;
  status: SessionStatus;
  run_started_at: string | null;
}

export interface SessionMessageRecord {
  id: string;
  // 中文注释：对话式调研改造后，消息表里还会出现 "tool" 角色（工具执行结果），
  // assistant 行也可能携带 tool_calls 元数据；聊天界面重建历史时需要识别它们。
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  created_at: string;
  reasoning?: string;
  media?: Array<Record<string, unknown>>;
  kind?: string;
  turn_id?: string | null;
  turn_phase?: string | null;
  turn_seq?: number | null;
  tool_calls?: Array<Record<string, unknown>>;
  tool_call_id?: string;
  tool_name?: string;
}

export interface StoredSessionEvent {
  id: string;
  event_type: string;
  content: string;
  created_at: string;
  seq_no: number;
  metadata: Record<string, unknown>;
}

export interface SessionArtifact {
  id: string;
  artifact_type: string;
  name: string;
  path: string;
  size: number;
  created_at: string;
  metadata: Record<string, unknown>;
}

export interface SessionThread {
  key: string;
  title: string;
  status: SessionStatus | string;
  messages: SessionMessageRecord[];
  events: StoredSessionEvent[];
  artifacts: SessionArtifact[];
  has_pending_tool_calls: boolean;
  run_started_at: string | null;
  /** 中文注释：当前还在跑的 run 编号，刷新页面后前端靠它接回实时流。为 null 表示没有在跑的。 */
  active_run_id: string | null;
}

export interface SessionListPayload {
  sessions: SessionSummary[];
}

export interface SessionCreatePayload {
  session: SessionSummary;
}

export interface SessionThreadPayload extends SessionThread {}

export interface SessionRunAccepted {
  session_key: string;
  run_id: string;
  turn_id: string;
  status: "accepted";
  stream_url: string;
}

export interface SessionRunStartPayload {
  content?: string;
  turn_id?: string;
}

export type RuntimeDetailContent = string | Record<string, unknown> | null;

export interface SessionRuntimeEvent {
  event: string;
  session_key: string;
  chat_id?: string;
  run_id?: string;
  turn_id?: string;
  timestamp?: string;
  stream_seq?: number;
  event_id?: string;
  role?: "user" | "assistant" | "system";
  kind?: string;
  content?: string;
  delta?: string;
  status?: string;
  run_started_at?: string | null;
  step?: string;
  id?: string;
  parent_id?: string | null;
  type?: string;
  title?: string;
  show_content?: string;
  detail_content?: RuntimeDetailContent;
  created_at?: string | null;
  updated_at?: string | null;
  completed_at?: string | null;
  node_key?: string;
  node_title?: string;
  stage?: string;
  media?: Array<Record<string, unknown>>;
  message?: string;
  artifact?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  keywords?: string[];
  sources?: string[];
  max_results?: number;
  raw_paper_count?: number;
  selected_paper_count?: number;
  artifact_count?: number;
  search_halted?: boolean;
  completed?: number;
  total?: number;
  input_tokens?: number;
  output_tokens?: number;
}

export interface UISessionMessage {
  id: string;
  role: "user" | "assistant" | "system";
  kind: string;
  content: string;
  reasoning: string;
  isStreaming: boolean;
  reasoningStreaming: boolean;
  media: Array<Record<string, unknown>>;
  toolEvents: SessionRuntimeEvent[];
  artifactRefs: Array<Record<string, unknown>>;
  turnId: string | null;
  createdAt: string | null;
  // 中文注释：卡片消息（paper_list / deep_read_report / review）的完整载荷，
  // 来自 message 事件的 metadata；普通文本消息为 null。
  card?: ChatCardPayload | null;
}

export interface UIRuntimeTimelineEvent {
  id: string;
  parentId: string | null;
  type: string;
  title: string;
  status: string;
  showContent: string;
  detailContent: RuntimeDetailContent;
  metadata: Record<string, unknown>;
  createdAt: string | null;
  updatedAt: string | null;
  completedAt: string | null;
  children: UIRuntimeTimelineEvent[];
  isCollapsed: boolean;
  completed: number | null;
  total: number | null;
  inputTokens: number;
  outputTokens: number;
  raw: SessionRuntimeEvent;
}

export interface SessionTimelineSnapshot {
  messages: UISessionMessage[];
  runtimeEvents: UIRuntimeTimelineEvent[];
  activeNodeKey: string | null;
  artifacts: SessionArtifact[];
  isStreaming: boolean;
  runStartedAt: string | null;
  streamError: SessionRuntimeEvent | null;
  status: string;
}
