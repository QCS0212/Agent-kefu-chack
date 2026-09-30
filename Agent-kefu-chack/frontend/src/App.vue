<script setup>
import { ref, onMounted } from 'vue'
import { api } from './api.js'
import Dashboard from './views/Dashboard.vue'
import Results from './views/Results.vue'
import Submit from './views/Submit.vue'
import Monitor from './views/Monitor.vue'

const tab = ref('dashboard')
const source = ref('local') // local | live
const health = ref(null)
const healthError = ref('')

const tabs = [
  { key: 'dashboard', label: '检测总览' },
  { key: 'results', label: '结果明细' },
  { key: 'submit', label: '提交检测' },
  { key: 'monitor', label: '线上监测' }
]

async function checkHealth () {
  health.value = null
  healthError.value = ''
  try {
    health.value = await api.health()
  } catch (e) {
    healthError.value = String(e.message || e)
  }
}

onMounted(() => { checkHealth() })
</script>

<template>
  <div class="page">
    <header class="topbar card">
      <div class="topbar-left">
        <div class="logo">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#2563eb" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>
          </svg>
        </div>
        <div>
          <h1 class="title">客服回复幻觉检测</h1>
          <div class="subtitle">批量核验客服回复的事实声明，对照知识库定位幻觉</div>
        </div>
      </div>
      <div class="topbar-right">
        <div class="source-switch" role="tablist">
          <button
            class="seg-btn"
            :class="{ active: source === 'local' }"
            @click="source = 'local'"
            role="tab"
            aria-selected="source === 'local'"
          >本地数据</button>
          <button
            class="seg-btn"
            :class="{ active: source === 'live' }"
            @click="source = 'live'"
            role="tab"
            aria-selected="source === 'live'"
          >在线接口</button>
        </div>
        <div v-if="source === 'live'" class="health">
          <span v-if="health" class="badge" :style="{ background: 'var(--ok-soft)', color: 'var(--ok)', borderColor: '#bbf7d0' }">
            接口正常 · v{{ health.version }}
          </span>
          <span v-else class="badge" :style="{ background: 'var(--danger-soft)', color: 'var(--danger)', borderColor: '#fecaca' }" :title="healthError">
            接口不可用
          </span>
          <button class="btn btn-xs" @click="checkHealth">刷新</button>
        </div>
        <span v-else class="badge" :style="{ background: 'var(--primary-soft)', color: 'var(--primary)', borderColor: '#bfdbfe' }">
          合并运行结果 · 20 条
        </span>
      </div>
    </header>

    <nav class="tabs">
      <button
        v-for="t in tabs"
        :key="t.key"
        class="tab-btn"
        :class="{ active: tab === t.key }"
        @click="tab = t.key"
      >{{ t.label }}</button>
    </nav>

    <main class="content">
      <Dashboard v-if="tab === 'dashboard'" :source="source" />
      <Results v-else-if="tab === 'results'" :source="source" />
      <Monitor v-else-if="tab === 'monitor'" />
      <Submit v-else :health="health" @submitted="source = 'live'" />
    </main>
  </div>
</template>

<style scoped>
.page { max-width: 1240px; margin: 0 auto; padding: 20px 24px 56px; }
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 20px;
}
.topbar-left { display: flex; align-items: center; gap: 12px; }
.logo { width: 36px; height: 36px; border-radius: 9px; background: var(--primary-soft); display: flex; align-items: center; justify-content: center; }
.title { font-size: 17px; }
.subtitle { font-size: 12px; color: var(--text-3); margin-top: 1px; }
.topbar-right { display: flex; align-items: center; gap: 12px; }
.source-switch { display: inline-flex; background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px; padding: 2px; }
.seg-btn {
  border: none; background: transparent; padding: 5px 14px; border-radius: 6px;
  font-size: 13px; font-weight: 600; color: var(--text-2);
}
.seg-btn.active { background: var(--surface); color: var(--primary); box-shadow: var(--shadow); }
.health { display: flex; align-items: center; gap: 8px; }
.btn-xs { padding: 3px 10px; font-size: 12px; }
.tabs { display: flex; gap: 4px; margin: 18px 0 14px; border-bottom: 1px solid var(--border); }
.tab-btn {
  border: none; background: transparent; padding: 9px 16px; font-size: 14px; font-weight: 600;
  color: var(--text-2); border-bottom: 2px solid transparent; margin-bottom: -1px;
}
.tab-btn.active { color: var(--primary); border-bottom-color: var(--primary); }
.tab-btn:hover { color: var(--text); }
@media (max-width: 720px) {
  .topbar { flex-direction: column; align-items: flex-start; }
  .page { padding: 14px; }
}
</style>
