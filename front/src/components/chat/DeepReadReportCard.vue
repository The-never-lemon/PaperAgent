<script setup lang="ts">
/**
 * 精读报告卡片（metadata.kind = "deep_read_report"）。
 *
 * 中文说明：
 * 精读完成后在对话流里出现的小卡片：总分、来源徽章（全文/摘要降级）、
 * 一句话总结和四个维度分数；点"查看完整报告"打开右侧抽屉。
 */
import { FileText } from "lucide-vue-next";

import type { DeepReadCardPayload } from "../../types/chat";

defineOptions({ name: "DeepReadReportCard" });

defineProps<{
  payload: DeepReadCardPayload;
}>();

const emit = defineEmits<{
  open: [payload: DeepReadCardPayload];
}>();
</script>

<template>
  <div class="chat-row chat-row-card">
    <section class="deep-read-card">
      <header class="deep-read-card-head">
        <FileText :size="16" />
        <h4 class="deep-read-card-title" :title="payload.report.title">{{ payload.report.title }}</h4>
        <span class="deep-read-source-badge" :data-source="payload.source">
          {{ payload.source === "fulltext" ? "全文精读" : "摘要降级" }}
        </span>
        <span class="deep-read-overall">{{ payload.report.overall_score }} 分</span>
      </header>
      <p v-if="payload.report.short_summary" class="deep-read-summary">{{ payload.report.short_summary }}</p>
      <div class="deep-read-dims">
        <span class="deep-read-dim">相关 {{ payload.report.relevance?.score ?? 0 }}</span>
        <span class="deep-read-dim">创新 {{ payload.report.novelty?.score ?? 0 }}</span>
        <span class="deep-read-dim">严谨 {{ payload.report.rigor?.score ?? 0 }}</span>
        <span class="deep-read-dim">清晰 {{ payload.report.clarity?.score ?? 0 }}</span>
      </div>
      <footer class="deep-read-card-actions">
        <button type="button" class="paper-card-action" @click="emit('open', payload)">
          <FileText :size="14" /> 查看完整报告
        </button>
      </footer>
    </section>
  </div>
</template>
