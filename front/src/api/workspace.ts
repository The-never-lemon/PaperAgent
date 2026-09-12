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
