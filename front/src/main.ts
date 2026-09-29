/** 文件作用：创建 Vue 应用，挂载页面路由并加载全局样式。 */

import { createApp } from "vue";

import App from "./App.vue";
import router from "./router";
import "./styles.css";

createApp(App).use(router).mount("#app");
