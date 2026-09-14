<script setup lang="ts">
/**
 * 聊天输入框。
 *
 * 中文说明：
 * 多行输入，Enter 发送、Shift+Enter 换行；运行中发送按钮变成"停止"按钮
 * （点了以后向后端发真正的取消请求）。欢迎态（variant=welcome）会显示
 * 刊名、大标题、书写栏和方向目录，用于还没有任何消息的新会话。
 */
import { computed, ref } from "vue";
import { LoaderCircle, SendHorizonal, Square } from "lucide-vue-next";

defineOptions({ name: "ChatComposer" });

const props = withDefaults(
  defineProps<{
    modelValue: string;
    running: boolean;
    sending: boolean;
    cancelling?: boolean;
    /** 中文注释：当前这次 run 是不是可以被取消的。没有 activeRunId 时是 false，
     * 此时停止按钮不显示，避免用户看到一个能点但毫无反应的「停止」。 */
    cancellable?: boolean;
    /** 中文注释：运行状态的中文说明（「正在连接」「助手工作中」），显示在输入框附近。 */
    statusText?: string;
    variant?: "default" | "welcome";
    heading?: string;
    helperText?: string;
    placeholder?: string;
    rows?: number;
  }>(),
  {
    cancelling: false,
    cancellable: false,
    statusText: "",
    variant: "default",
    heading: "开始一次论文调研对话",
    helperText: "",
    placeholder: "例如：帮我调研 LLM 推理优化的最新论文",
    rows: 2,
  },
);

const emit = defineEmits<{
  "update:modelValue": [value: string];
  submit: [];
  cancel: [];
}>();

const textareaElement = ref<HTMLTextAreaElement | null>(null);

const canSubmit = computed(() => Boolean(props.modelValue.trim()) && !props.sending && !props.running);
const isWelcome = computed(() => props.variant === "welcome");

/**
 * 欢迎态的快速开始示例。
 * 中文说明：左边写短标题（像目录里的条目名），右边写领域提示；
 * 真正发给助手的是 prompt 整句。点一下就发出去，新用户不用自己琢磨第一句。
 */
const quickStarts = [
  {
    title: "KV Cache 压缩",
    hint: "推理加速",
    prompt: "帮我调研大语言模型推理加速里的 KV Cache 压缩方法",
  },
  {
    title: "LLM 推理优化",
    hint: "近两年",
    prompt: "调研 LLM 推理优化的最新论文，检索 5 篇左右并评价相关性",
  },
  {
    title: "医学图像分割",
    hint: "扩散模型",
    prompt: "我想了解扩散模型在医学图像分割中的应用，近两年的",
  },
  {
    title: "多智能体协作",
    hint: "代表性工作",
    prompt: "帮我梳理多智能体协作（multi-agent）方向的代表性工作",
  },
];

/** 点击快速开始：把示例文字填进输入框并立即发送。 */
function useQuickStart(text: string) {
  if (props.sending || props.running) {
    return;
  }
  emit("update:modelValue", text);
  // 中文注释：等 v-model 的更新写回父组件后再触发提交，保证发送的是示例全文。
  requestAnimationFrame(() => emit("submit"));
}

/** 把输入框内容更新回父组件。 */
function onInput(event: Event) {
  const target = event.target as HTMLTextAreaElement;
  emit("update:modelValue", target.value);
}

/** Enter 直接发送，Shift+Enter 换行。 */
function onKeydown(event: KeyboardEvent) {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (canSubmit.value) {
      emit("submit");
    }
  }
}

/** 发送后让输入框重新获得焦点，方便连续对话。 */
function focusInput() {
  requestAnimationFrame(() => textareaElement.value?.focus());
}

defineExpose({ focusInput });
</script>

<template>
  <div class="chat-composer" :data-variant="variant">
    <!-- 中文说明：欢迎页从上往下排：刊名和提问在上，输入框和方向目录在下。 -->
    <div :class="isWelcome ? 'chat-welcome-spread' : undefined">
      <div v-if="isWelcome" class="chat-welcome-copy">
        <div class="chat-welcome-masthead">
          <img class="welcome-logo" src="/logo.svg?v=2" alt="" />
          <span>PaperAgent</span>
        </div>
        <div class="chat-welcome-lead">
          <h2>{{ heading }}</h2>
          <p v-if="helperText">{{ helperText }}</p>
        </div>
      </div>

      <div :class="isWelcome ? 'chat-welcome-desk' : undefined">
        <div class="chat-composer-box">
          <textarea
            ref="textareaElement"
            class="chat-composer-input"
            :value="modelValue"
            :rows="rows"
            :placeholder="placeholder"
            :disabled="sending"
            @input="onInput"
            @keydown="onKeydown"
          ></textarea>
          <div class="chat-composer-actions">
            <!-- 中文说明：欢迎页空闲时不写「准备就绪」，那几个字不帮用户做事，只占位置。
                 改成键盘提示；真的在发送或停止时，再换成原来的状态文字。 -->
            <span v-if="isWelcome && !statusText" class="chat-welcome-keyhint">Enter 发送，Shift + Enter 换行</span>
            <span v-else-if="statusText" class="chat-composer-status">{{ statusText }}</span>
            <!-- 中文注释：只有 cancellable 为 true 时才显示停止按钮，避免刷新后
                 activeRunId 丢失时用户看到一个能点但毫无反应的「停止」。 -->
            <button
              v-if="running && cancellable"
              type="button"
              class="chat-composer-cancel"
              :disabled="cancelling"
              @click="emit('cancel')"
            >
              <LoaderCircle v-if="cancelling" class="spinning" :size="15" />
              <Square v-else :size="13" />
              <span>{{ cancelling ? "正在停止" : "停止" }}</span>
            </button>
            <button
              type="button"
              class="chat-composer-submit"
              :disabled="!canSubmit"
              @click="emit('submit')"
            >
              <LoaderCircle v-if="sending" class="spinning" :size="15" />
              <SendHorizonal v-else :size="15" />
              <span>发送</span>
            </button>
          </div>
        </div>

        <div v-if="isWelcome" class="chat-welcome-toc">
          <p class="chat-welcome-toc-label">或从这些方向开始</p>
          <button
            v-for="topic in quickStarts"
            :key="topic.prompt"
            type="button"
            class="chat-welcome-toc-item"
            :disabled="sending || running"
            @click="useQuickStart(topic.prompt)"
          >
            <span class="chat-welcome-toc-title">{{ topic.title }}</span>
            <span class="chat-welcome-toc-hint">{{ topic.hint }}</span>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
