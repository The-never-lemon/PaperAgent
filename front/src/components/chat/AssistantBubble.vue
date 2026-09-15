<script setup lang="ts">
/**
 * 助手消息气泡。
 *
 * 中文说明：
 * 靠左显示助手每一轮的正文；正文用 MarkdownText 组件渲染（模型输出天然是
 * Markdown：加粗、列表、代码块等都能正常显示，且不使用 v-html、无注入风险）。
 * 如果有思考过程（reasoning），折叠在气泡顶部；流式思考时自动展开。
 * 流式输出时正文末尾带一个闪烁光标，思考区显示"思考中"。
 */
import { ref, watch } from "vue";
import { Brain, ChevronDown } from "lucide-vue-next";

import MarkdownText from "./MarkdownText.vue";
import MathText from "./MathText.vue";

defineOptions({ name: "AssistantBubble" });

const props = defineProps<{
  content: string;
  reasoning?: string;
  isStreaming?: boolean;
  reasoningStreaming?: boolean;
  /** 工作区里真实存在的 paper_id 集合，传给 MarkdownText 做引用按钮渲染。 */
  knownPaperIds?: Set<string>;
}>();

const emit = defineEmits<{
  paperClick: [paperId: string];
}>();

/** 思考过程默认收起；正在流式输出思考时自动展开，结束后保持展开方便回看。 */
const reasoningExpanded = ref(false);

watch(
  () => props.reasoningStreaming,
  (streaming) => {
    if (streaming) {
      reasoningExpanded.value = true;
    }
  },
  { immediate: true },
);
</script>

<template>
  <div class="chat-row chat-row-assistant">
    <div class="chat-bubble chat-bubble-assistant">
      <button
        v-if="reasoning || reasoningStreaming"
        type="button"
        class="chat-reasoning-toggle"
        :aria-expanded="reasoningExpanded"
        @click="reasoningExpanded = !reasoningExpanded"
      >
        <Brain :size="14" />
        <span>{{ reasoningStreaming ? "思考中…" : "思考过程" }}</span>
        <ChevronDown :size="14" :class="{ expanded: reasoningExpanded }" />
      </button>
      <pre v-if="(reasoning || reasoningStreaming) && reasoningExpanded" class="chat-reasoning-body"><MathText :text="reasoning || ''" /></pre>

      <MarkdownText
        v-if="content"
        :content="content"
        :streaming="isStreaming"
        :known-paper-ids="knownPaperIds"
        @paper-click="(pid) => emit('paperClick', pid)"
      />
      <p v-if="!content && isStreaming" class="chat-bubble-placeholder">
        正在组织回复…<span class="chat-cursor" aria-hidden="true"></span>
      </p>
    </div>
  </div>
</template>
