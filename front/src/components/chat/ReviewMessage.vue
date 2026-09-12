<script setup lang="ts">
/**
 * 综述产物卡片（metadata.kind = "review"）。
 *
 * 中文说明：
 * 综述生成完成后展示主题、字数和章节目录，并提供终稿 Markdown 的
 * 下载/预览链接（走会话产物端点）。
 */
import { computed } from "vue";
import { FileDown, ScrollText } from "lucide-vue-next";

import type { ReviewCardPayload } from "../../types/chat";

defineOptions({ name: "ReviewMessage" });

const props = defineProps<{
  payload: ReviewCardPayload;
  sessionKey: string;
}>();

/** 拼接综述终稿的下载地址。 */
const downloadUrl = computed(() => {
  if (!props.sessionKey || !props.payload.artifact_id) return "#";
  return `/api/sessions/${encodeURIComponent(props.sessionKey)}/artifacts/${encodeURIComponent(props.payload.artifact_id)}`;
});
</script>

<template>
  <div class="chat-row chat-row-card">
    <section class="review-card">
      <header class="review-card-head">
        <ScrollText :size="16" />
        <h4 class="review-card-title">综述已生成：{{ payload.topic }}</h4>
        <span class="review-card-meta">{{ payload.word_count }} 字 · {{ payload.sections.length }} 节</span>
      </header>
      <ol v-if="payload.sections.length" class="review-card-sections">
        <li v-for="section in payload.sections" :key="section.section_id">
          <code>{{ section.section_id }}</code> {{ section.title }}
        </li>
      </ol>
      <footer class="review-card-actions">
        <a class="paper-card-action review-download" :href="downloadUrl" target="_blank" rel="noreferrer">
          <FileDown :size="14" /> 下载 / 预览 literature_review.md
        </a>
      </footer>
    </section>
  </div>
</template>
