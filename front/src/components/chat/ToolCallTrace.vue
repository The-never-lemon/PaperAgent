<script setup lang="ts">
/**
 * 工具执行痕迹（对话流里的"执行过程"折叠区）。
 *
 * 中文说明：
 * 复用现有的 RuntimeEventTree 组件渲染运行事件树（检索/评价/精读/综述的
 * 每一张卡片都在里面，含状态三态和 token 用量），外面包一层可折叠容器，
 * 默认收起，运行中自动展开，让用户随时能看到"助手正在做什么"。
 */
import { computed, ref, watch } from "vue";
import { ListTree } from "lucide-vue-next";

import RuntimeEventTree from "../session/RuntimeEventTree.vue";
import type { UIRuntimeTimelineEvent } from "../../types/sessions";

defineOptions({ name: "ToolCallTrace" });

const props = defineProps<{
  events: UIRuntimeTimelineEvent[];
  /** 是否处于运行中（运行中自动展开）。 */
  active?: boolean;
  /**
   * 中文注释：卡片上"继续"按钮的处理函数。后端在失败或停止的综述卡片上下发了
   * 可续跑编号，点了就接着写。正在跑的时候父组件不会传这个函数，按钮就不显示。
   */
  onResume?: (threadId: string) => void;
}>();

const expanded = ref(Boolean(props.active));

// 运行状态变化时同步展开/收起：开始执行自动展开，结束后自动收起留一行摘要。
// 中文注释：下面用 v-show 而不是 v-if，收起时不把事件树拆掉，下次展开或
// 新工具卡片进来时动画和折叠状态都还在。
watch(
  () => props.active,
  (value) => {
    expanded.value = Boolean(value);
  },
);

const summaryText = computed(() => {
  const total = props.events.length;
  const running = props.events.some((event) => event.status === "running" || event.status === "pending");
  if (running) return "正在执行工具…";
  return `执行过程（${total} 个步骤）`;
});
</script>

<template>
  <div v-if="events.length" class="chat-row chat-row-trace">
    <section class="tool-trace">
      <button type="button" class="tool-trace-toggle" :aria-expanded="expanded" @click="expanded = !expanded">
        <ListTree :size="14" />
        <span>{{ summaryText }}</span>
        <span class="tool-trace-chevron" :class="{ expanded: expanded }">▾</span>
      </button>
      <div v-show="expanded" class="tool-trace-body">
        <RuntimeEventTree :events="events" :on-resume="onResume" />
      </div>
    </section>
  </div>
</template>
