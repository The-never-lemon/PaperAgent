<script setup lang="ts">
/**
 * 精读报告抽屉（右侧滑出面板 + 追问输入框）。
 *
 * 中文说明：
 * 展示一份完整的 DeepReadReport：核心问题、方法、数据集、贡献、局限、
 * 主要结果、实验设置、结论、四个维度评分与总分。
 * 底部的追问框把问题发回对话流（由主 Agent 调 ask_paper 工具基于
 * 报告 + 全文回答），提交后抽屉自动收起，答案以助手气泡形式出现在聊天里。
 */
import { ref, watch } from "vue";
import { FileDown, LoaderCircle, SendHorizonal, X } from "lucide-vue-next";

import type { DeepReadReportPayload } from "../../types/chat";
import MathText from "./MathText.vue";

defineOptions({ name: "DeepReadDrawer" });

const props = defineProps<{
  visible: boolean;
  report: DeepReadReportPayload | null;
  /** 所属会话编号，拼报告 JSON 下载地址用。 */
  sessionKey?: string;
  /** 追问请求发出后禁用输入，避免一轮 run 里重复提问。 */
  busy?: boolean;
}>();

const emit = defineEmits<{
  close: [];
  ask: [question: string];
}>();

const question = ref("");

// 每次切换论文时清空上一次的追问草稿。
watch(
  () => props.report?.paper_id,
  () => {
    question.value = "";
  },
);

/** 提交追问：问题非空且不在运行中才允许发送。 */
function submitQuestion() {
  const text = question.value.trim();
  if (!text || props.busy) return;
  emit("ask", text);
  question.value = "";
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    submitQuestion();
  }
}

/** 维度评分列表（模板里循环渲染，避免四段重复 markup）。 */
function dimensions(report: DeepReadReportPayload) {
  return [
    { label: "相关性", value: report.relevance },
    { label: "创新性", value: report.novelty },
    { label: "严谨性", value: report.rigor },
    { label: "清晰度", value: report.clarity },
  ];
}

/** 报告 JSON 文件的下载地址（artifact 缺失时隐藏按钮）。 */
function reportUrl(sessionKey: string, artifactId: string) {
  if (!sessionKey || !artifactId) return "";
  return `/api/sessions/${encodeURIComponent(sessionKey)}/artifacts/${encodeURIComponent(artifactId)}`;
}
</script>

<template>
  <Teleport to="body">
    <div v-if="visible" class="deep-read-drawer-backdrop" @click.self="emit('close')">
      <aside class="deep-read-drawer" role="dialog" aria-modal="true" aria-label="精读报告">
        <header class="deep-read-drawer-head">
          <div>
            <span class="deep-read-source-badge" :data-source="report?.source">
              {{ report?.source === "fulltext" ? "全文精读" : "摘要降级" }}
            </span>
            <h3><MathText :text="report?.title || '精读报告'" /></h3>
          </div>
          <button type="button" class="deep-read-drawer-close" aria-label="关闭" @click="emit('close')">
            <X :size="18" />
          </button>
        </header>

        <div v-if="report" class="deep-read-drawer-body">
          <section class="drawer-section drawer-score-section">
            <div class="drawer-overall">
              <strong>{{ report.overall_score }}</strong>
              <span>综合评分</span>
            </div>
            <ul class="drawer-dims">
              <li v-for="dim in dimensions(report)" :key="dim.label">
                <span>{{ dim.label }}</span>
                <strong>{{ dim.value?.score ?? 0 }}</strong>
                <small v-if="dim.value?.rationale"><MathText :text="dim.value.rationale" /></small>
              </li>
            </ul>
            <p v-if="report.overall_comment" class="drawer-comment"><MathText :text="report.overall_comment" /></p>
          </section>

          <section v-if="report.short_summary" class="drawer-section">
            <h4>一段话总结</h4>
            <p><MathText :text="report.short_summary" /></p>
          </section>
          <section v-if="report.main_question" class="drawer-section">
            <h4>核心问题</h4>
            <p><MathText :text="report.main_question" /></p>
          </section>
          <section v-if="report.methods.length" class="drawer-section">
            <h4>方法</h4>
            <ul><li v-for="(item, index) in report.methods" :key="index"><MathText :text="item" /></li></ul>
          </section>
          <section v-if="report.datasets.length" class="drawer-section">
            <h4>数据集</h4>
            <ul><li v-for="(item, index) in report.datasets" :key="index"><MathText :text="item" /></li></ul>
          </section>
          <section v-if="report.contributions.length" class="drawer-section">
            <h4>贡献</h4>
            <ul><li v-for="(item, index) in report.contributions" :key="index"><MathText :text="item" /></li></ul>
          </section>
          <section v-if="report.main_results.length" class="drawer-section">
            <h4>主要结果</h4>
            <ul><li v-for="(item, index) in report.main_results" :key="index"><MathText :text="item" /></li></ul>
          </section>
          <section v-if="report.experimental_setup" class="drawer-section">
            <h4>实验设置</h4>
            <p><MathText :text="report.experimental_setup" /></p>
          </section>
          <section v-if="report.conclusions" class="drawer-section">
            <h4>结论</h4>
            <p><MathText :text="report.conclusions" /></p>
          </section>
          <section v-if="report.limitations.length" class="drawer-section">
            <h4>局限</h4>
            <ul><li v-for="(item, index) in report.limitations" :key="index"><MathText :text="item" /></li></ul>
          </section>

          <footer class="drawer-footer">
            <a
              v-if="reportUrl(sessionKey ?? '', report.artifact_id)"
              class="paper-card-action"
              :href="reportUrl(sessionKey ?? '', report.artifact_id)"
              target="_blank"
              rel="noreferrer"
            >
              <FileDown :size="14" /> 下载报告 JSON
            </a>
            <span class="drawer-created-at">生成于 {{ report.created_at?.slice(0, 19).replace("T", " ") }}</span>
          </footer>
        </div>
        <div v-else class="deep-read-drawer-body drawer-empty">
          <LoaderCircle class="spinning" :size="18" />
          <span>正在加载报告…</span>
        </div>

        <footer v-if="report" class="deep-read-drawer-ask">
          <textarea
            v-model="question"
            class="drawer-ask-input"
            rows="2"
            placeholder="基于报告和全文追问，例如：它的实验用了什么基线？"
            :disabled="busy"
            @keydown="onKeydown"
          ></textarea>
          <button type="button" class="chat-composer-submit" :disabled="!question.trim() || busy" @click="submitQuestion">
            <SendHorizonal :size="15" />
            <span>追问</span>
          </button>
        </footer>
      </aside>
    </div>
  </Teleport>
</template>
