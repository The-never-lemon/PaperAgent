/**
 * 会话工作区 REST 客户端（对应后端 routers/workspace.py）。
 *
 * 中文说明：
 * 聊天界面刷新恢复时，除了消息流（webui-thread），论文清单和精读报告
 * 直接从工作区端点读取，保证卡片抽屉随时能打开最新数据。
 * 包 4 新增：用户标注（PATCH）、批量删除（DELETE）、导出（GET /export）。
 */

import type { WorkspaceReportPayload, WorkspaceSnapshot } from "../types/chat";

/** 与 api/sessions.ts 相同的最小请求封装：统一 JSON 解析与错误消息。 */
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  const data = (await response.json().catch(() => ({}))) as {
    error?: { message?: string };
    detail?: string | { message?: string };
  } & T;
  if (!response.ok) {
    const detailMessage = typeof data.detail === "string" ? data.detail : data.detail?.message;
    throw new Error(data.error?.message || detailMessage || "请求失败");
  }
  return data as T;
}

/** 读取会话工作区快照（研究主题 + 论文清单）。 */
export function fetchWorkspace(sessionKey: string): Promise<WorkspaceSnapshot> {
  return request<WorkspaceSnapshot>(`/api/sessions/${encodeURIComponent(sessionKey)}/workspace`);
}

/**
 * 读取单篇论文的精读报告；论文不存在或尚未精读时后端返回 404。
 * 中文注释：paper_id 可能带斜杠（DOI 编号），后端路由用 :path 转换器接收，
 * 这里做标准 URL 编码即可（%2F 会被服务端还原成斜杠再整体匹配）。
 */
export function fetchPaperReport(sessionKey: string, paperId: string): Promise<WorkspaceReportPayload> {
  return request<WorkspaceReportPayload>(
    `/api/sessions/${encodeURIComponent(sessionKey)}/workspace/papers/${encodeURIComponent(paperId)}/report`,
  );
}

/** 更新论文的用户标注（加星、标签、笔记）。不启动 run，不消耗模型。 */
export function updatePaperAnnotations(
  sessionKey: string,
  paperId: string,
  updates: { starred?: boolean; tags?: string[]; note?: string },
): Promise<{ paper_id: string; starred: boolean; tags: string[]; note: string }> {
  return request(
    `/api/sessions/${encodeURIComponent(sessionKey)}/workspace/papers/${encodeURIComponent(paperId)}`,
    { method: "PATCH", body: JSON.stringify(updates) },
  );
}

/** 上传一篇本地 PDF 后，后端返回的核对信息。 */
export interface UploadPaperResult {
  paper_id: string;
  title: string;
  authors: string[];
  year: number | null;
  /** true 表示新加入；false 表示工作区里本来就有这一篇（同一个文件重复上传）。 */
  is_new: boolean;
  page_count: number | null;
  /** false 说明这份 PDF 读不出文字（扫描版）；null 表示判断不了。 */
  has_text_layer: boolean | null;
  /** 从 PDF 第一页认出来的摘要，认不出就是空字符串。 */
  abstract: string;
  /** 一句给用户看的中文提示，直接显示即可。 */
  notice: string;
}

/**
 * 上传本地 PDF 到论文工作区。
 *
 * 中文说明：
 * 这里不能套用上面的 request()，因为它写死了 Content-Type: application/json。
 * 上传文件必须让浏览器自己生成 multipart 的分隔标记（boundary），手动写
 * Content-Type 会把 boundary 漏掉，后端就解析不出文件。所以单独写一个
 * 不设 Content-Type 的请求，其余错误处理逻辑和 request() 保持一致。
 */
export async function uploadPaper(sessionKey: string, file: File): Promise<UploadPaperResult> {
  const form = new FormData();
  // 中文注释：第三个参数是文件名。不显式传的话，某些浏览器会把它丢掉，
  // 后端就拿不到原始文件名，认不出标题时只能写成一个默认名字。
  form.append("file", file, file.name);
  const response = await fetch(
    `/api/sessions/${encodeURIComponent(sessionKey)}/workspace/papers/upload`,
    { method: "POST", body: form },
  );
  const data = (await response.json().catch(() => ({}))) as {
    error?: { message?: string };
    detail?: string | unknown[];
  } & Partial<UploadPaperResult>;
  if (!response.ok) {
    // 中文注释：后端正常报错时 detail 是一句中文；请求格式不对时（比如根本没带文件）
    // FastAPI 返回的 detail 是一个数组，这里兜一句通用提示，免得界面显示成 [object Object]。
    const detail = typeof data.detail === "string" ? data.detail : "";
    throw new Error(data.error?.message || detail || "上传失败，请重试");
  }
  return data as UploadPaperResult;
}

/**
 * 更新论文的元数据（标题、作者、年份、摘要）。不启动 run，不消耗模型。
 * 中文注释：用户上传 PDF 后，在确认框里改标题这些信息走这里。
 * 它和上面的 updatePaperAnnotations 是同一个后端接口，只是改的字段不同。
 */
export function updatePaperMetadata(
  sessionKey: string,
  paperId: string,
  meta: { title?: string; authors?: string[]; year?: number | null; abstract?: string },
): Promise<{ paper_id: string; title: string; authors: string[]; year: number | null; abstract: string }> {
  return request(
    `/api/sessions/${encodeURIComponent(sessionKey)}/workspace/papers/${encodeURIComponent(paperId)}`,
    { method: "PATCH", body: JSON.stringify(meta) },
  );
}

/** 批量删除工作区论文。不启动 run，不消耗模型。 */
export function batchRemovePapers(sessionKey: string, paperIds: string[]): Promise<{ removed: number }> {
  return request(`/api/sessions/${encodeURIComponent(sessionKey)}/workspace/papers`, {
    method: "DELETE",
    body: JSON.stringify({ paper_ids: paperIds }),
  });
}

/** 导出工作区论文清单。format 可以是 bibtex / markdown / csv。 */
export function exportWorkspace(
  sessionKey: string,
  format: "bibtex" | "markdown" | "csv",
  paperIds?: string[],
): string {
  const params = new URLSearchParams({ format });
  if (paperIds && paperIds.length > 0) {
    params.set("paper_ids", paperIds.join(","));
  }
  return `/api/sessions/${encodeURIComponent(sessionKey)}/workspace/export?${params.toString()}`;
}
