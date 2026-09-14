import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  server: {
    // 默认只监听本机地址，避免 Windows 上 localhost 先走 ::1，
    // 刚好又有别的进程占着同一个端口时，浏览器打开出来是 404。
    // 如果需要让同一局域网里的其它设备访问前端，可以执行 npm run dev:network。
    host: "127.0.0.1",
    port: 5173,
    // 端口被占用时直接报错，避免看起来还是 5173，实际却连到了别的服务。
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        // 中文注释：对话流是 SSE。默认代理超时会在模型/工具执行的静默期掐断连接，
        // 前端看起来就像每次调用工具都整页刷新一次。
        timeout: 0,
        proxyTimeout: 0,
      },
      "/webui": "http://127.0.0.1:8000",
      // 中文注释：Swagger 文档页面和它拉取接口定义用的路径都要转发到后端，
      // 否则页面里的「API 文档」链接打开后是一片空白。
      "/docs": "http://127.0.0.1:8000",
      "/openapi.json": "http://127.0.0.1:8000",
      "/redoc": "http://127.0.0.1:8000",
    },
  },
});
