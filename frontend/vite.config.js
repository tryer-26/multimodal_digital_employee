import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 后端服务地址（开发 / 预览环境代理目标）
// Docker 部署时通过 BACKEND_TARGET 环境变量指向 core 服务（如 http://core:8000）
const BACKEND_TARGET = process.env.BACKEND_TARGET || 'http://localhost:8000'

// 代理规则：/api 走 REST，/ws 走 WebSocket
const proxy = {
  '/api': {
    target: BACKEND_TARGET,
    changeOrigin: true
  },
  '/ws': {
    target: BACKEND_TARGET,
    changeOrigin: true,
    ws: true
  }
}

// Vite 配置：开发/预览端口 3000，代理 /api 与 /ws 到后端
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    }
  },
  server: {
    port: 3000,
    host: true,
    proxy
  },
  // 容器部署用 `vite preview` 提供静态产物，同样需要代理后端接口
  preview: {
    port: 3000,
    host: true,
    proxy
  }
})
