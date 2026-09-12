<script setup lang="ts">
import { notifications, removeToast } from "../stores/notifications";
</script>

<template>
  <div class="toast-stack" aria-live="polite">
    <article
      v-for="item in notifications.items"
      :key="item.id"
      class="toast"
      :data-tone="item.tone"
    >
      <div class="toast-copy">
        <strong>{{ item.title }}</strong>
        <p v-if="item.description">{{ item.description }}</p>
      </div>
      <div class="toast-actions">
        <button
          v-if="item.action"
          class="toast-action-button"
          type="button"
          @click="item.action.onClick()"
        >
          {{ item.action.label }}
        </button>
        <button class="icon-button ghost" type="button" @click="removeToast(item.id)">
          关闭
        </button>
      </div>
    </article>
  </div>
</template>
