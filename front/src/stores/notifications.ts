import { reactive } from "vue";

export type ToastTone = "success" | "error" | "info";

export interface ToastAction {
  label: string;
  onClick: () => void;
}

export interface ToastMessage {
  id: number;
  title: string;
  description?: string;
  tone: ToastTone;
  /** 可选的 action 按钮（例如「重试」）。 */
  action?: ToastAction;
  /** 自动消失时间（毫秒）。0 表示不自动消失，默认 3200。 */
  duration?: number;
}

export const notifications = reactive({
  items: [] as ToastMessage[],
});

let nextId = 1;

export function pushToast(message: Omit<ToastMessage, "id">) {
  const item: ToastMessage = {
    id: nextId++,
    ...message,
  };
  notifications.items.push(item);
  // duration 为 0 表示不自动消失；否则用指定时长或默认 3200ms。
  const duration = message.duration ?? 3200;
  if (duration > 0) {
    window.setTimeout(() => {
      removeToast(item.id);
    }, duration);
  }
}

export function removeToast(id: number) {
  const index = notifications.items.findIndex((item) => item.id === id);
  if (index >= 0) {
    notifications.items.splice(index, 1);
  }
}
