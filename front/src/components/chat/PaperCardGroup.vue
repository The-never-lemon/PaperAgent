<script setup lang="ts">
/**
 * 论文卡片组（metadata.kind = "paper_list"）。
 *
 * 中文说明：
 * 检索完成后展示"新增 X 篇 / 重复跳过 Y 篇"和论文卡片网格；
 * 评价完成后（action == "evaluate"）展示带分数徽章的卡片。
 * 每张卡片提供两个动作：
 * 1. "精读"——把请求发回聊天输入流（由主 Agent 调 deep_read_paper 工具）；
 * 2. "报告"——已精读的论文直接打开精读报告抽屉。
 */
import { computed } from "vue";
import { BookOpenCheck, ExternalLink, FileText } from "lucide-vue-next";

import { pickPaperLink } from "../../lib/paper-link";
import type { ChatPaperCard, PaperListPayload } from "../../types/chat";

defineOptions({ name: "PaperCardGroup" });

const props = defineProps<{
  payload: PaperListPayload;
  /** 当前会话里已经精读过、可以直接打开报告的论文编号集合。 */
  readablePaperIds?: Set<string>;
  /** 运行中禁用卡片按钮，避免一次 run 里塞进多条指令。 */
  busy?: boolean;
}>();

const emit = defineEmits<{
  deepRead: [paper: ChatPaperCard];
  openReport: [paper: ChatPaperCard];
}>();

const isEvaluate = computed(() => props.payload.action === "evaluate");

const headerText = computed(() => {
  if (isEvaluate.value) {
    return `评价完成：${props.payload.evaluated ?? props.payload.papers.length} 篇已出分`;
  }
  const added = props.payload.added ?? 0;
  const duplicated = props.payload.duplicated ?? 0;
  return `检索完成：新增 ${added} 篇${duplicated ? `，重复跳过 ${duplicated} 篇` : ""}`;
});

/** 分数对应的徽章色调：高分绿、中分黄、低分灰。 */
function scoreTone(score: number | null | undefined) {
  if (score === null || score === undefined) return "neutral";
  if (score >= 60) return "good";
  if (score >= 30) return "mid";
  return "low";
}

function statusLabel(status?: string) {
  if (status === "deep_read") return "已精读";
  if (status === "evaluated") return "已评价";
  return "未评价";
}
</script>

<template>
  <div class="chat-row chat-row-card">
    <section class="paper-card-group">
      <header class="paper-card-group-head">
        <span class="paper-card-group-title">{{ headerText }}</span>
        <code v-if="payload.query" class="paper-card-group-query">{{ payload.query }}</code>
      </header>
      <div class="paper-card-grid">
        <article v-for="paper in payload.papers" :key="paper.paper_id" class="paper-card">
          <header class="paper-card-title-row">
            <h4 class="paper-card-title" :title="paper.title">{{ paper.title }}</h4>
            <span v-if="paper.score !== null && paper.score !== undefined" class="paper-score" :data-tone="scoreTone(paper.score)">
              {{ paper.score }} 分
            </span>
          </header>
          <p class="paper-card-meta">
            <span v-if="paper.authors.length">{{ paper.authors.slice(0, 3).join(", ") }}{{ paper.authors.length > 3 ? " 等" : "" }}</span>
            <span v-if="paper.year">{{ paper.year }}</span>
            <span v-if="paper.venue">{{ paper.venue }}</span>
            <span class="paper-source-badge">{{ paper.source || "未知来源" }}</span>
            <span class="paper-status-badge">{{ statusLabel(paper.status) }}</span>
          </p>
          <p v-if="paper.abstract" class="paper-card-abstract">{{ paper.abstract }}</p>
          <footer class="paper-card-actions">
            <button
              type="button"
              class="paper-card-action"
              :disabled="busy"
              @click="emit('deepRead', paper)"
            >
              <BookOpenCheck :size="14" /> 精读
            </button>
            <button
              v-if="paper.status === 'deep_read' || readablePaperIds?.has(paper.paper_id)"
              type="button"
              class="paper-card-action"
              @click="emit('openReport', paper)"
            >
              <FileText :size="14" /> 报告
            </button>
            <a
              v-if="pickPaperLink(paper)"
              class="paper-card-action paper-card-link"
              :href="pickPaperLink(paper)"
              target="_blank"
              rel="noreferrer"
            >
              <ExternalLink :size="14" /> 原文
            </a>
          </footer>
        </article>
      </div>
    </section>
  </div>
</template>
