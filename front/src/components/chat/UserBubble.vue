<script setup lang="ts">
/**
 * 用户消息气泡。
 *
 * 中文说明：靠右显示用户发出的每一条消息；正文按原样保留换行。
 */
defineOptions({ name: "UserBubble" });

defineProps<{
  content: string;
  createdAt?: string | null;
}>();

const emit = defineEmits<{
  resend: [content: string];
}>();

/** 把 ISO 时间变成"14:05"这样的短时间，仅用于气泡角标。 */
function formatTime(value?: string | null) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(date);
}
</script>

<template>
  <div class="chat-row chat-row-user">
    <div class="chat-bubble chat-bubble-user">
      <p class="chat-bubble-text">{{ content }}</p>
      <span v-if="formatTime(createdAt)" class="chat-bubble-time">{{ formatTime(createdAt) }}</span>
      <button
        type="button"
        class="chat-resend-button"
        @click="emit('resend', content)"
        title="重新发送"
      >
        ↻
      </button>
    </div>
  </div>
</template>
