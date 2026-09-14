<script setup lang="ts">
import {
  CheckCircle2,
  ChevronRight,
  CircleDot,
  LoaderCircle,
  RotateCw,
  TriangleAlert,
} from "lucide-vue-next";

import StatusPill from "../StatusPill.vue";
import type { UIRuntimeTimelineEvent } from "../../types/sessions";
import MathText from "../chat/MathText.vue";

defineOptions({
  name: "RuntimeEventTree",
});

defineProps<{
  events: UIRuntimeTimelineEvent[];
  depth?: number;
  /**
   * 中文注释：卡片上"继续"按钮的处理函数，一路透传给下一层（这个组件会自己渲染
   * 自己，用函数当属性比一层层往上抛事件省事）。正在运行的时候不传，按钮就不出现。
   */
  onResume?: (threadId: string) => void;
}>();

/**
 * 中文注释：后端在"流程做到一半失败了"或"用户点了停止"的卡片上会下发一个
 * resume_thread_id，含义是"这一步留下了可以接着写的地方"，有它就显示"继续"。
 *
 * 为什么要限定 workflow_step（具体步骤卡片）：失败时后端会同时更新"具体步骤"
 * 和它上面那张"汇总"卡片，同一个编号两处都会带上。只认具体步骤那张，卡片树里
 * 才不会冒出两个一模一样的"继续"。
 */
function resumeThreadId(event: UIRuntimeTimelineEvent): string {
  if (event.type !== "workflow_step") {
    return "";
  }
  const value = event.metadata?.resume_thread_id;
  return typeof value === "string" ? value.trim() : "";
}

function statusTone(status: string) {
  if (status === "completed" || status === "success") {
    return "success";
  }
  if (status === "running" || status === "pending" || status === "cancel_requested") {
    return "warning";
  }
  if (status === "cancelled") {
    return "neutral";
  }
  if (status === "failed" || status === "error") {
    return "danger";
  }
  return "neutral";
}

function statusLabel(status: string) {
  if (status === "running") {
    return "处理中";
  }
  if (status === "cancel_requested") {
    return "正在停止";
  }
  if (status === "cancelled") {
    return "已停止";
  }
  if (status === "completed") {
    return "完成";
  }
  if (status === "failed") {
    return "失败";
  }
  if (status === "pending") {
    return "等待中";
  }
  if (status === "skipped") {
    return "已跳过";
  }
  return status || "未知";
}

function statusIcon(status: string) {
  if (status === "completed" || status === "success") {
    return CheckCircle2;
  }
  if (status === "failed" || status === "error") {
    return TriangleAlert;
  }
  if (status === "running" || status === "pending" || status === "cancel_requested") {
    return LoaderCircle;
  }
  return CircleDot;
}

/** 中文注释：这里统一选一个最能代表事件当前状态的时间，避免每行出现多个时间把界面挤乱。 */
function eventTime(event: UIRuntimeTimelineEvent) {
  return event.completedAt ?? event.updatedAt ?? event.createdAt;
}

/** 使用本地时间渲染事件时间戳。 */
function formatTime(value: string | null) {
  if (!value) {
    return "";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

/** 中文注释：detailContent 可以是字符串，也可以是对象；对象格式化后更方便用户展开查看。 */
function formatDetailContent(value: UIRuntimeTimelineEvent["detailContent"]) {
  if (!value) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  return JSON.stringify(value, null, 2);
}

function hasDetail(event: UIRuntimeTimelineEvent) {
  if (!event.detailContent) {
    return false;
  }
  if (typeof event.detailContent === "string") {
    return Boolean(event.detailContent.trim());
  }
  return Object.keys(event.detailContent).length > 0;
}
</script>

<template>
  <ol class="runtime-event-tree" :style="{ '--event-depth': depth ?? 0 }">
    <li
      v-for="event in events"
      :key="event.id"
      class="runtime-event-item"
      :data-status="event.status"
    >
      <details
        v-if="event.children.length"
        class="runtime-event-branch"
        :open="!event.isCollapsed"
      >
        <summary class="runtime-event-row">
          <span class="runtime-event-toggle">
            <ChevronRight :size="14" />
          </span>
          <span class="runtime-event-dot">
            <component
              :is="statusIcon(event.status)"
              :size="14"
              :class="{ spinning: event.status === 'running' }"
            />
          </span>
          <span class="runtime-event-copy">
            <span class="runtime-event-title-line">
              <strong><MathText :text="event.title" /></strong>
              <StatusPill :tone="statusTone(event.status)" :label="statusLabel(event.status)" />
              <button
                v-if="onResume && resumeThreadId(event)"
                type="button"
                class="runtime-event-resume"
                @click.stop.prevent="onResume?.(resumeThreadId(event))"
              >
                <RotateCw :size="12" />
                <span>继续</span>
              </button>
            </span>
            <span class="runtime-event-show"><MathText :text="event.showContent" /></span>
          </span>
          <span class="runtime-event-meta">
            <time class="runtime-event-time">{{ formatTime(eventTime(event)) }}</time>
            <span class="runtime-event-tokens">输入 {{ event.inputTokens }} · 输出 {{ event.outputTokens }}</span>
          </span>
        </summary>

        <details v-if="hasDetail(event)" class="runtime-event-detail">
          <summary>查看详情</summary>
          <pre>{{ formatDetailContent(event.detailContent) }}</pre>
        </details>

        <RuntimeEventTree
          class="runtime-event-children"
          :events="event.children"
          :depth="(depth ?? 0) + 1"
          :on-resume="onResume"
        />
      </details>

      <div v-else class="runtime-event-row runtime-event-row-leaf">
        <span class="runtime-event-toggle" aria-hidden="true"></span>
        <span class="runtime-event-dot">
          <component
            :is="statusIcon(event.status)"
            :size="14"
            :class="{ spinning: event.status === 'running' }"
          />
        </span>
        <span class="runtime-event-copy">
          <span class="runtime-event-title-line">
            <strong><MathText :text="event.title" /></strong>
            <StatusPill :tone="statusTone(event.status)" :label="statusLabel(event.status)" />
            <button
              v-if="onResume && resumeThreadId(event)"
              type="button"
              class="runtime-event-resume"
              @click.stop="onResume?.(resumeThreadId(event))"
            >
              <RotateCw :size="12" />
              <span>继续</span>
            </button>
          </span>
          <span class="runtime-event-show"><MathText :text="event.showContent" /></span>
          <details v-if="hasDetail(event)" class="runtime-event-detail">
            <summary>查看详情</summary>
            <pre>{{ formatDetailContent(event.detailContent) }}</pre>
          </details>
        </span>
        <span class="runtime-event-meta">
          <time class="runtime-event-time">{{ formatTime(eventTime(event)) }}</time>
          <span class="runtime-event-tokens">输入 {{ event.inputTokens }} · 输出 {{ event.outputTokens }}</span>
        </span>
      </div>
    </li>
  </ol>
</template>
