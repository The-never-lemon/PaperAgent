<script setup lang="ts">
/**
 * 常驻论文工作区面板（包 4 新增）。
 *
 * 中文说明：
 * 这是对话页右侧的可折叠面板，列出当前会话的全部论文。支持：
 * - 筛选：状态（全部/未评价/已评价/已精读）、来源、加星
 * - 排序：加入时间（默认）、评分、年份
 * - 搜索：标题/作者关键词过滤
 * - 多选：勾选后批量精读、删除、导出
 * - 单篇动作：加星、打标签、写笔记、精读、打开报告
 *
 * 数据来源：GET /api/sessions/{key}/workspace（后端已实现）。
 * 动作分流：纯状态动作（加星/删除/导出）走 PATCH/DELETE REST，不启动 run；
 *           需要 Agent 产出的动作（精读）走主 Agent 工具。
 */
import { computed, ref, watch } from "vue";
import { Star, Trash2, Download, BookOpenCheck, FileText, X, Filter, SortAsc } from "lucide-vue-next";

import type { WorkspacePaperItem, WorkspaceSnapshot } from "../../types/chat";
import { batchRemovePapers, exportWorkspace, fetchWorkspace, updatePaperAnnotations } from "../../api/workspace";
import { pushToast } from "../../stores/notifications";

defineOptions({ name: "PaperLibraryPanel" });

const props = defineProps<{
  sessionKey: string;
  /** 运行中禁用批量动作按钮，避免和正在跑的 Agent 冲突。 */
  busy?: boolean;
}>();

const emit = defineEmits<{
  /** 请求精读某篇论文（交给主 Agent 处理）。 */
  requestDeepRead: [paperId: string];
  /** 打开某篇论文的精读报告抽屉。 */
  openReport: [paperId: string];
  /** 面板里的论文列表变了（增删后），通知 ChatView 刷新卡片状态。 */
  workspaceChanged: [];
}>();

// ---------------------------------------------------------------------------
// 数据加载
// ---------------------------------------------------------------------------

const snapshot = ref<WorkspaceSnapshot | null>(null);
const loading = ref(false);

async function refresh() {
  if (!props.sessionKey) {
    snapshot.value = null;
    return;
  }
  loading.value = true;
  try {
    snapshot.value = await fetchWorkspace(props.sessionKey);
  } catch (err) {
    pushToast({ tone: "error", title: "加载工作区失败", description: (err as Error).message });
  } finally {
    loading.value = false;
  }
}

watch(() => props.sessionKey, refresh, { immediate: true });

// 暴露给父组件调用的刷新方法（turn_end 时调用，保证面板和对话流同步）。
defineExpose({ refresh });

// ---------------------------------------------------------------------------
// 筛选 / 排序 / 搜索
// ---------------------------------------------------------------------------

const statusFilter = ref<"all" | "new" | "evaluated" | "deep_read">("all");
const sourceFilter = ref<string>("all");
const starredOnly = ref(false);
const sortBy = ref<"added_at" | "score" | "year">("added_at");
const searchQuery = ref("");

const sources = computed(() => {
  if (!snapshot.value) return [];
  const set = new Set<string>();
  for (const p of snapshot.value.papers) {
    if (p.source) set.add(p.source);
  }
  return Array.from(set).sort();
});

const filteredPapers = computed(() => {
  if (!snapshot.value) return [];
  let list = snapshot.value.papers;

  // 状态筛选。
  if (statusFilter.value !== "all") {
    list = list.filter((p) => p.status === statusFilter.value);
  }
  // 来源筛选。
  if (sourceFilter.value !== "all") {
    list = list.filter((p) => p.source === sourceFilter.value);
  }
  // 只看加星。
  if (starredOnly.value) {
    list = list.filter((p) => p.starred);
  }
  // 关键词搜索（标题 + 作者）。
  if (searchQuery.value.trim()) {
    const q = searchQuery.value.toLowerCase();
    list = list.filter((p) => {
      const title = (p.title || "").toLowerCase();
      const authors = (p.authors || []).join(" ").toLowerCase();
      return title.includes(q) || authors.includes(q);
    });
  }

  // 排序。
  const sorted = [...list];
  if (sortBy.value === "score") {
    sorted.sort((a, b) => (b.score ?? -1) - (a.score ?? -1));
  } else if (sortBy.value === "year") {
    sorted.sort((a, b) => (b.year ?? 0) - (a.year ?? 0));
  } else {
    // added_at 降序（最新的在前）。
    sorted.sort((a, b) => (b.added_at > a.added_at ? 1 : -1));
  }
  return sorted;
});

// ---------------------------------------------------------------------------
// 多选
// ---------------------------------------------------------------------------

const selectedIds = ref<Set<string>>(new Set());

function toggleSelect(paperId: string) {
  const next = new Set(selectedIds.value);
  if (next.has(paperId)) {
    next.delete(paperId);
  } else {
    next.add(paperId);
  }
  selectedIds.value = next;
}

function selectAll() {
  selectedIds.value = new Set(filteredPapers.value.map((p) => p.paper_id));
}

function clearSelection() {
  selectedIds.value = new Set();
}

// ---------------------------------------------------------------------------
// 动作：加星 / 删除 / 导出
// ---------------------------------------------------------------------------

async function toggleStar(paper: WorkspacePaperItem) {
  try {
    await updatePaperAnnotations(props.sessionKey, paper.paper_id, { starred: !paper.starred });
    paper.starred = !paper.starred;
  } catch (err) {
    pushToast({ tone: "error", title: "加星失败", description: (err as Error).message });
  }
}

async function deleteSelected() {
  const ids = Array.from(selectedIds.value);
  if (ids.length === 0) return;
  if (!confirm(`确定删除 ${ids.length} 篇论文？`)) return;
  try {
    const result = await batchRemovePapers(props.sessionKey, ids);
    pushToast({ tone: "success", title: `已删除 ${result.removed} 篇` });
    selectedIds.value = new Set();
    await refresh();
    emit("workspaceChanged");
  } catch (err) {
    pushToast({ tone: "error", title: "删除失败", description: (err as Error).message });
  }
}

function downloadSelected(format: "bibtex" | "markdown" | "csv") {
  const ids = selectedIds.value.size > 0 ? Array.from(selectedIds.value) : undefined;
  const url = exportWorkspace(props.sessionKey, format, ids);
  // 用 a 标签触发下载。
  const a = document.createElement("a");
  a.href = url;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

// ---------------------------------------------------------------------------
// 动作：精读 / 打开报告（交给父组件处理）
// ---------------------------------------------------------------------------

function requestDeepRead(paperId: string) {
  emit("requestDeepRead", paperId);
}

function openReport(paperId: string) {
  emit("openReport", paperId);
}

// ---------------------------------------------------------------------------
// 辅助函数
// ---------------------------------------------------------------------------

function statusLabel(status: string): string {
  if (status === "deep_read") return "已精读";
  if (status === "evaluated") return "已评价";
  return "未评价";
}
</script>

<template>
  <aside class="paper-library-panel">
    <header class="panel-header">
      <h3>论文工作区</h3>
      <span v-if="snapshot" class="paper-count">{{ snapshot.papers.length }} 篇</span>
    </header>

    <!-- 筛选栏 -->
    <div class="filter-bar">
      <div class="filter-row">
        <select v-model="statusFilter" class="filter-select">
          <option value="all">全部状态</option>
          <option value="new">未评价</option>
          <option value="evaluated">已评价</option>
          <option value="deep_read">已精读</option>
        </select>
        <select v-model="sourceFilter" class="filter-select">
          <option value="all">全部来源</option>
          <option v-for="src in sources" :key="src" :value="src">{{ src }}</option>
        </select>
      </div>
      <div class="filter-row">
        <label class="starred-toggle">
          <input type="checkbox" v-model="starredOnly" />
          <Star :size="14" :class="{ filled: starredOnly }" />
          只看加星
        </label>
        <select v-model="sortBy" class="filter-select">
          <option value="added_at">按加入时间</option>
          <option value="score">按评分</option>
          <option value="year">按年份</option>
        </select>
      </div>
      <input
        v-model="searchQuery"
        type="search"
        placeholder="搜索标题或作者…"
        class="search-input"
      />
    </div>

    <!-- 多选工具栏 -->
    <div v-if="selectedIds.size > 0" class="selection-bar">
      <span>已选 {{ selectedIds.size }} 篇</span>
      <button type="button" class="selection-action" @click="selectAll">全选</button>
      <button type="button" class="selection-action" @click="clearSelection">取消</button>
      <button
        type="button"
        class="selection-action danger"
        :disabled="busy"
        @click="deleteSelected"
      >
        <Trash2 :size="14" /> 删除
      </button>
      <div class="export-group">
        <button
          type="button"
          class="selection-action"
          @click="downloadSelected('bibtex')"
          title="导出为 BibTeX"
        >
          <Download :size="14" /> BibTeX
        </button>
        <button
          type="button"
          class="selection-action"
          @click="downloadSelected('markdown')"
          title="导出为 Markdown"
        >
          <Download :size="14" /> MD
        </button>
        <button
          type="button"
          class="selection-action"
          @click="downloadSelected('csv')"
          title="导出为 CSV"
        >
          <Download :size="14" /> CSV
        </button>
      </div>
    </div>

    <!-- 论文列表 -->
    <div class="paper-list" v-if="!loading && filteredPapers.length > 0">
      <div
        v-for="paper in filteredPapers"
        :key="paper.paper_id"
        class="paper-item"
        :class="{ selected: selectedIds.has(paper.paper_id) }"
      >
        <input
          type="checkbox"
          :checked="selectedIds.has(paper.paper_id)"
          @change="toggleSelect(paper.paper_id)"
          class="paper-checkbox"
        />
        <div class="paper-main">
          <div class="paper-title-row">
            <button
              type="button"
              class="star-button"
              :class="{ starred: paper.starred }"
              @click="toggleStar(paper)"
              :title="paper.starred ? '取消加星' : '加星'"
            >
              <Star :size="14" :class="{ filled: paper.starred }" />
            </button>
            <h4 class="paper-title" :title="paper.title">{{ paper.title }}</h4>
          </div>
          <p class="paper-meta">
            <span v-if="paper.authors.length">{{ paper.authors.slice(0, 2).join(", ") }}{{ paper.authors.length > 2 ? " 等" : "" }}</span>
            <span v-if="paper.year">{{ paper.year }}</span>
            <span class="status-badge" :data-status="paper.status">{{ statusLabel(paper.status) }}</span>
            <span v-if="paper.score !== null" class="score-badge">{{ paper.score }} 分</span>
          </p>
          <div class="paper-actions">
            <button
              v-if="paper.status !== 'deep_read'"
              type="button"
              class="paper-action"
              :disabled="busy"
              @click="requestDeepRead(paper.paper_id)"
            >
              <BookOpenCheck :size="13" /> 精读
            </button>
            <button
              v-if="paper.has_report"
              type="button"
              class="paper-action"
              @click="openReport(paper.paper_id)"
            >
              <FileText :size="13" /> 报告
            </button>
            <a
              v-if="paper.url"
              :href="paper.url"
              target="_blank"
              rel="noreferrer"
              class="paper-action link"
            >
              原文 ↗
            </a>
          </div>
        </div>
      </div>
    </div>

    <div v-else-if="loading" class="panel-loading">加载中…</div>
    <div v-else-if="snapshot && snapshot.papers.length === 0" class="panel-empty">
      工作区暂无论文，先在对话里检索。
    </div>
    <div v-else-if="filteredPapers.length === 0" class="panel-empty">
      没有匹配的论文，调整筛选条件试试。
    </div>
  </aside>
</template>
