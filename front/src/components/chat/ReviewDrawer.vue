<!-- 文件作用：从页面侧边打开完整综述，并提供 Markdown 下载。 -->

<script setup lang="ts">
/**
 * 综述抽屉（右侧滑出面板）。
 *
 * 中文说明：
 * 和精读报告抽屉一样：卡片上只放摘要，点「查看完整综述」再把终稿拉过来显示。
 * 终稿是已经存好的 Markdown 文件，这里读出来排成标题和段落；下载按钮让浏览器
 * 把同一份文件存到本地，而不是在新标签页里打开一堆原文。
 */
import { ref, watch } from "vue";
import { FileDown, LoaderCircle, X } from "lucide-vue-next";

import type { ReviewCardPayload } from "../../types/chat";
import MarkdownText from "./MarkdownText.vue";

defineOptions({ name: "ReviewDrawer" });

const props = defineProps<{
  visible: boolean;
  payload: ReviewCardPayload | null;
  sessionKey?: string;
  paperRefLookup?: Map<string, string>;
  knownPaperIds?: Set<string>;
}>();

const emit = defineEmits<{
  close: [];
  paperClick: [paperId: string];
}>();

const markdown = ref("");
const loading = ref(false);
const errorText = ref("");

/** 拼综述终稿地址。download 为真时让浏览器把文件存下来。 */
function reviewFileUrl(sessionKey: string, artifactId: string, download: boolean) {
  if (!sessionKey || !artifactId) return "";
  const path = `/api/sessions/${encodeURIComponent(sessionKey)}/artifacts/${encodeURIComponent(artifactId)}`;
  return download ? `${path}?download=1` : path;
}

// 每次打开或换一篇综述，重新把终稿读进来。关掉时丢掉上一份，避免闪一下旧内容。
let loadSeq = 0;
watch(
  () => [props.visible, props.sessionKey, props.payload?.artifact_id] as const,
  async ([visible, sessionKey, artifactId]) => {
    const seq = ++loadSeq;
    markdown.value = "";
    errorText.value = "";
    if (!visible || !sessionKey || !artifactId) {
      loading.value = false;
      return;
    }
    loading.value = true;
    try {
      const response = await fetch(reviewFileUrl(sessionKey, artifactId, false));
      if (!response.ok) {
        throw new Error("读取综述失败");
      }
      const text = await response.text();
      if (seq !== loadSeq) return;
      markdown.value = text;
    } catch (error) {
      if (seq !== loadSeq) return;
      errorText.value = error instanceof Error ? error.message : "读取综述失败";
    } finally {
      if (seq === loadSeq) loading.value = false;
    }
  },
);
</script>

<template>
  <Teleport to="body">
    <div v-if="visible" class="deep-read-drawer-backdrop" @click.self="emit('close')">
      <aside class="deep-read-drawer" role="dialog" aria-modal="true" aria-label="文献综述">
        <header class="deep-read-drawer-head">
          <div>
            <span class="deep-read-source-badge">文献综述</span>
            <h3>{{ payload?.topic || "文献综述" }}</h3>
            <p v-if="payload" class="review-drawer-meta">
              {{ payload.word_count }} 字 · {{ payload.sections.length }} 节
            </p>
          </div>
          <button type="button" class="deep-read-drawer-close" aria-label="关闭" @click="emit('close')">
            <X :size="18" />
          </button>
        </header>

        <div v-if="loading" class="deep-read-drawer-body drawer-empty">
          <LoaderCircle class="spinning" :size="18" />
          <span>正在加载综述…</span>
        </div>
        <div v-else-if="errorText" class="deep-read-drawer-body drawer-empty">
          <span>{{ errorText }}</span>
        </div>
        <div v-else-if="markdown" class="deep-read-drawer-body">
          <MarkdownText
            :content="markdown"
            :paper-ref-lookup="paperRefLookup"
            :known-paper-ids="knownPaperIds"
            @paper-click="emit('paperClick', $event)"
          />
          <footer class="drawer-footer">
            <a
              v-if="reviewFileUrl(sessionKey ?? '', payload?.artifact_id ?? '', true)"
              class="paper-card-action"
              :href="reviewFileUrl(sessionKey ?? '', payload?.artifact_id ?? '', true)"
              download="literature_review.md"
            >
              <FileDown :size="14" /> 下载综述 Markdown
            </a>
          </footer>
        </div>
        <div v-else class="deep-read-drawer-body drawer-empty">
          <span>还没有可显示的综述正文。</span>
        </div>
      </aside>
    </div>
  </Teleport>
</template>
