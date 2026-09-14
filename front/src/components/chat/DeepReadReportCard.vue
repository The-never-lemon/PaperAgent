<script setup lang="ts">
/**
 * 精读报告卡片（metadata.kind = "deep_read_report"）。
 *
 * 中文说明：
 * 精读完成后在对话流里出现的小卡片：总分、来源徽章（全文/摘要降级）、
 * 一句话总结和四个维度分数；点"查看完整报告"打开右侧抽屉。
 * 卡片本身只拿预览字段，完整报告由父组件去工作区再取。
 */
import { FileText } from "lucide-vue-next";

import type { DeepReadCardPayload } from "../../types/chat";
import MathText from "./MathText.vue";

defineOptions({ name: "DeepReadReportCard" });

const props = defineProps<{
  payload: DeepReadCardPayload;
}>();

const emit = defineEmits<{
  open: [payload: DeepReadCardPayload];
}>();

/** 中文说明：把四个维度整理成「标签 + 分数」两块，模板里循环渲染，
 *  避免四段几乎一样的标记。标签用简称，卡片比较窄，长名字放不下。 */
function dimensions(payload: DeepReadCardPayload) {
  return [
    { label: "相关", value: payload.relevance },
    { label: "创新", value: payload.novelty },
    { label: "严谨", value: payload.rigor },
    { label: "清晰", value: payload.clarity },
  ];
}
</script>

<template>
  <div class="chat-row chat-row-card">
    <section class="deep-read-card">
      <header class="deep-read-card-head">
        <FileText :size="16" />
        <h4 class="deep-read-card-title" :title="payload.title"><MathText :text="payload.title" /></h4>
        <span
          class="deep-read-source-badge"
          :data-source="payload.source"
          :title="payload.fulltext_failure_reason || ''"
        >
          {{ payload.source === "fulltext" ? "全文精读" : "无法下载全文" }}
        </span>
        <span class="deep-read-overall">{{ payload.overall_score }} 分</span>
      </header>
      <!-- 中文注释：没拿到全文时给一句明确的说明，不让用户以为下面的报告是通读全文写出来的。 -->
      <p v-if="payload.source !== 'fulltext'" class="deep-read-warning">
        本篇论文无法下载全文{{ payload.fulltext_failure_reason ? `（${payload.fulltext_failure_reason}）` : "" }}，
        以下报告基于标题和摘要生成，未通读全文。
      </p>
      <p v-if="payload.short_summary" class="deep-read-summary"><MathText :text="payload.short_summary" /></p>
      <div class="deep-read-dims">
        <span v-for="dim in dimensions(props.payload)" :key="dim.label" class="deep-read-dim">
          <span class="deep-read-dim-label">{{ dim.label }}</span>
          <strong class="deep-read-dim-score">{{ dim.value ?? 0 }}</strong>
        </span>
      </div>
      <footer class="deep-read-card-actions">
        <button type="button" class="paper-card-action" @click="emit('open', payload)">
          <FileText :size="14" /> 查看完整报告
        </button>
      </footer>
    </section>
  </div>
</template>
