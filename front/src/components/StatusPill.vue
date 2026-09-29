<!-- 文件作用：用彩色小标签显示运行、连接或任务当前状态。 -->

<script setup lang="ts">
defineProps<{
  tone?: "neutral" | "success" | "warning" | "danger";
  label: string;
}>();
</script>

<template>
  <span class="status-pill" :data-tone="tone ?? 'neutral'">
    {{ label }}
  </span>
</template>
