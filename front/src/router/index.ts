import { createRouter, createWebHistory } from "vue-router";

import ChatView from "../views/ChatView.vue";
import SystemSettingsView from "../views/SystemSettingsView.vue";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: "/",
      redirect: "/sessions",
    },
    {
      // 中文注释：对话式调研改造后，会话主界面从旧工作台换成聊天流 ChatView；
      // 路由名与 props 契约保持不变，App.vue 的侧边栏联动无需改动。
      path: "/sessions",
      name: "sessions",
      component: ChatView,
    },
    {
      path: "/settings",
      name: "settings",
      component: SystemSettingsView,
    },
  ],
});

export default router;
