<!-- 文件作用：展示综述生成进度、章节状态和最终产物。 -->

<script setup lang="ts">
/**
 * 综述产物卡片（metadata.kind = "review"）。
 *
 * 中文说明：
 * 综述生成完成后展示主题、字数和章节目录。点「查看完整综述」打开右侧抽屉，
 * 在抽屉里阅读全文并下载 Markdown。卡片本身不塞整篇终稿。
 */
import { FileText, ScrollText } from "lucide-vue-next";

import type { ReviewCardPayload } from "../../types/chat";
import MathText from "./MathText.vue";

defineOptions({ name: "ReviewMessage" });

defineProps<{
  payload: ReviewCardPayload;
}>();

const emit = defineEmits<{
  open: [payload: ReviewCardPayload];
}>();
</script>

<template>
  <div class="chat-row chat-row-card">
    <section class="review-card">
      <header class="review-card-head">
        <ScrollText :size="16" />
        <h4 class="review-card-title">综述已生成：<MathText :text="payload.topic" /></h4>
        <span class="review-card-meta">{{ payload.word_count }} 字 · {{ payload.sections.length }} 节</span>
      </header>
      <ol v-if="payload.sections.length" class="review-card-sections">
        <li v-for="section in payload.sections" :key="section.section_id">
          <code>{{ section.section_id }}</code> <MathText :text="section.title" />
        </li>
      </ol>
      <footer class="review-card-actions">
        <button
          type="button"
          class="paper-card-action"
          :disabled="!payload.artifact_id"
          @click="emit('open', payload)"
        >
          <FileText :size="14" /> 查看完整综述
        </button>
      </footer>
    </section>
  </div>
</template>
