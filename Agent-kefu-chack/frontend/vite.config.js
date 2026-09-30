import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const API_TARGET = 'http://127.0.0.1:8000'
const MONITOR_TARGET = 'http://127.0.0.1:8010'
const stripOrigin = (proxy) => {
  // 后端中间件只允许本机无 Origin 的请求，代理转发时去掉浏览器来源头
  proxy.on('proxyReq', (proxyReq) => proxyReq.removeHeader('origin'))
}

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      // 监测平台接口先匹配，避免被 /api 规则吃掉
      '/api/monitor': { target: MONITOR_TARGET, changeOrigin: false, configure: stripOrigin },
      '/api/ingest': { target: MONITOR_TARGET, changeOrigin: false, configure: stripOrigin },
      '/api': { target: API_TARGET, changeOrigin: false, configure: stripOrigin },
      '/health': { target: API_TARGET, changeOrigin: false, configure: stripOrigin }
    }
  }
})