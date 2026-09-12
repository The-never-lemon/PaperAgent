<script setup lang="ts">
/**
 * 论文信息弹窗。
 *
 * 中文说明：
 * 点击助手回复里的 [paper_id] 引用、而这篇文章还没精读时弹出这个浮层，
 * 展示标题、作者年份来源、摘要，以及一个可以打开原文的按钮。
 * 已经精读过的文章不会走这里——那种情况直接打开精读报告抽屉。
 *
 * 关于"原文"链接：数据源给的 pdf_url 并不总是真 PDF——OpenAlex 找不到 PDF 时会把
 * DOI 跳转地址填进去（形如 https://doi.org/10.xxxx/yyyy）。DOI 地址点开是落地页，
 * 装了 sci-hub 之类插件的浏览器还会把它劫持到第三方站点。所以挑链接时跳过 DOI 地址，
 * 优先真实的 PDF 直链，其次用检索源自己的论文页面（arXiv 摘要页、OpenAlex / S2 详情页）。
 *
 * 这是一个独立浮层，不会往对话流里插入任何内容。
 */
import { onBeforeUnmount, watch } from "vue";
import { BookOpenCheck, ExternalLink, X } from "lucide-vue-next";

import { pickPaperLink } from "../../lib/paper-link";
import type { WorkspacePaperItem } from "../../types/chat";

defineOptions({ name: "PaperInfoDialog" });

const props = defineProps<{
  visible: boolean;
  paper: WorkspacePaperItem | null;
  /** 运行中禁用"精读"按钮，避免一次 run 里塞进多条指令。 */
  busy?: boolean;
}>();

const emit = defineEmits<{
  close: [];
  deepRead: [paperId: string];
}>();

/** 论文原文地址：跳过 DOI 跳转地址，优先开放获取 PDF 直链，其次检索源的论文页面。 */
function paperLink(paper: WorkspacePaperItem) {
  return pickPaperLink(paper);
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === "Escape") {
    emit("close");
  }
}

// 中文注释：弹窗打开时挂上 ESC 关窗的监听，关闭后立刻摘掉，不让监听一直留在页面上。
watch(
  () => props.visible,
  (visible) => {
    if (visible) {
      window.addEventListener("keydown", onKeydown);
    } else {
      window.removeEventListener("keydown", onKeydown);
    }
  },
);

onBeforeUnmount(() => window.removeEventListener("keydown", onKeydown));
</script>

<template>
  <Teleport to="body">
    <div v-if="visible && paper" class="paper-dialog-backdrop" @click.self="emit('close')">
      <section class="paper-dialog" role="dialog" aria-modal="true" aria-label="论文信息">
        <header class="paper-dialog-head">
          <span class="paper-source-badge">{{ paper.source || "未知来源" }}</span>
          <button type="button" class="paper-dialog-close" aria-label="关闭" @click="emit('close')">
            <X :size="18" />
          </button>
        </header>

        <h3 class="paper-dialog-title">{{ paper.title }}</h3>

        <p class="paper-card-meta paper-dialog-meta">
          <span v-if="paper.authors.length">{{ paper.authors.slice(0, 3).join(", ") }}{{ paper.authors.length > 3 ? " 等" : "" }}</span>
          <span v-if="paper.year">{{ paper.year }}</span>
          <span v-if="paper.venue">{{ paper.venue }}</span>
        </p>

        <p v-if="paper.abstract" class="paper-dialog-abstract">{{ paper.abstract }}</p>
        <p v-else class="paper-dialog-abstract paper-dialog-empty">
          检索源没有提供这篇论文的摘要。
        </p>

        <footer class="paper-dialog-actions">
          <a
            v-if="paperLink(paper)"
            class="paper-card-action paper-card-link"
            :href="paperLink(paper)"
            target="_blank"
            rel="noreferrer"
          >
            <ExternalLink :size="14" /> 原文
          </a>
          <button
            type="button"
            class="paper-card-action"
            :disabled="busy"
            @click="emit('deepRead', paper.paper_id)"
          >
            <BookOpenCheck :size="14" /> 精读
          </button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>
