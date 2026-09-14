/**
 * 对话式调研的前端协议类型（与后端冻结协议一一对应）。
 *
 * 中文说明：
 * 后端在阶段 1 冻结了 SSE message 事件的 metadata.kind 协议：
 * - kind = "text"             助手正文（气泡）
 * - kind = "paper_list"       论文卡片组（检索/评价后推送）
 * - kind = "deep_read_report" 精读报告卡片（只带标题、总结和分数；完整报告走工作区接口）
 * - kind = "review"           综述产物卡片
 * 这个文件是协议的前端唯一权威定义：后端协议任何变更都必须同步到这里，
 * 并在提交信息里标注 [protocol]。
 */

/** 会话卡片消息的三种类型。 */
export type ChatCardKind = "paper_list" | "deep_read_report" | "review";

/** 论文卡片里的单篇论文（检索卡片与评价卡片共用；评价卡片多 score/status 两个字段）。 */
export interface ChatPaperCard {
  paper_id: string;
  title: string;
  authors: string[];
  year: number | null;
  venue: string;
  source: string;
  abstract: string;
  url: string;
  pdf_url: string;
  has_pdf: boolean;
  /** 三维评分总分（仅评价后的卡片携带）。 */
  score?: number | null;
  /** 论文处理状态：new / evaluated / deep_read（仅评价后的卡片携带）。 */
  status?: string;
  /** 这篇论文是从本机以前的全文精读里召回来的。 */
  recalled?: boolean;
}

/** metadata.kind = "paper_list" 的载荷（检索与评价共用，action 区分）。 */
export interface PaperListPayload {
  kind: "paper_list";
  papers: ChatPaperCard[];
  /** 检索场景：新增数量。 */
  added?: number;
  /** 检索场景：重复跳过数量。 */
  duplicated?: number;
  /** 检索场景：从本机历史精读里召回的篇数。 */
  recalled?: number;
  /** 检索场景：本次检索式。 */
  query?: string;
  /** 评价场景固定为 "evaluate"。 */
  action?: string;
  /** 评价场景：成功出分数量。 */
  evaluated?: number;
}

/** 精读报告里的单维度评分。 */
export interface DeepReadDimension {
  score: number;
  rationale: string;
}

/** 精读报告全量结构（与后端 src/models/deep_read.py 的 DeepReadReport 字段一致）。 */
export interface DeepReadReportPayload {
  schema_version?: number;
  paper_id: string;
  title: string;
  source: "fulltext" | "abstract_fallback" | string;
  main_question: string;
  methods: string[];
  datasets: string[];
  contributions: string[];
  limitations: string[];
  main_results: string[];
  short_summary: string;
  experimental_setup: string;
  conclusions: string;
  relevance: DeepReadDimension;
  novelty: DeepReadDimension;
  rigor: DeepReadDimension;
  clarity: DeepReadDimension;
  overall_score: number;
  overall_comment: string;
  created_at: string;
  artifact_id: string;
  fulltext_artifact_id: string;
}

/** metadata.kind = "deep_read_report" 的载荷。
 *  卡片只带预览：标题、一句话总结和分数。完整报告点开后再向工作区要。 */
export interface DeepReadCardPayload {
  kind: "deep_read_report";
  paper_id: string;
  source: string;
  artifact_id: string;
  /** 全文是否真的拿到了；false 表示这篇论文没能下载到全文，报告基于摘要生成。 */
  fulltext_available?: boolean;
  /** 没拿到全文时的原因，例如"该论文未提供开放获取的全文链接""下载全文超时"。 */
  fulltext_failure_reason?: string;
  title: string;
  short_summary: string;
  overall_score: number;
  relevance: number;
  novelty: number;
  rigor: number;
  clarity: number;
}

/** 综述产物里的章节条目。 */
export interface ReviewSectionItem {
  section_id: string;
  title: string;
}

/** metadata.kind = "review" 的载荷。 */
export interface ReviewCardPayload {
  kind: "review";
  artifact_id: string;
  word_count: number;
  sections: ReviewSectionItem[];
  topic: string;
}

/** 三种卡片载荷的联合类型（UISessionMessage.card 字段用）。 */
export type ChatCardPayload = PaperListPayload | DeepReadCardPayload | ReviewCardPayload;

// ---------------------------------------------------------------------------
// 工作区 REST 接口的响应类型（GET /{key}/workspace 与 report 端点）
// ---------------------------------------------------------------------------

/** 工作区快照里的单篇论文。 */
export interface WorkspacePaperItem {
  paper_id: string;
  title: string;
  authors: string[];
  year: number | null;
  venue: string;
  source: string;
  abstract: string;
  url: string;
  pdf_url: string;
  status: string;
  score: number | null;
  has_report: boolean;
  fulltext_cached: boolean;
  added_at: string;
  /** 用户标注：是否加星（schema v2 新增）。 */
  starred: boolean;
  /** 用户标注：标签列表（schema v2 新增）。 */
  tags: string[];
  /** 用户标注：笔记（schema v2 新增）。 */
  note: string;
}

/** GET /api/sessions/{key}/workspace 的响应。 */
export interface WorkspaceSnapshot {
  session_key: string;
  research_topic: string;
  updated_at: string;
  total: number;
  papers: WorkspacePaperItem[];
}

/** GET /api/sessions/{key}/workspace/papers/{paper_id}/report 的响应。 */
export interface WorkspaceReportPayload {
  session_key: string;
  paper_id: string;
  report: DeepReadReportPayload;
}
