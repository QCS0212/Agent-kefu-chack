<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { api } from '../api.js'
import { CATEGORIES, VERDICTS, SEVERITIES, verdictMeta, categoryMeta } from '../constants.js'
import VChart from '../use-echarts.js'
import MetricCards from '../components/MetricCards.vue'

const props = defineProps({ source: { type: String, default: 'local' } })

const loading = ref(false)
const error = ref('')
const run = ref(null) // { results, evaluation, model, mode, total }
const taskId = ref(localStorage.getItem('lastTaskId') || '')

const results = computed(() => run.value?.results || [])
const evaluation = computed(() => run.value?.evaluation || null)
const metrics = computed(() => evaluation.value?.metrics || null)

function loadLocal () {
  loading.value = true
  error.value = ''
  Promise.all([
    fetch('/data/predictions.json').then((r) => r.json()),
    fetch('/data/metrics.json').then((r) => r.json())
  ])
    .then(([preds, ev]) => {
      run.value = {
        results: preds.results || [],
        evaluation: ev,
        model: preds.model,
        mode: preds.mode,
        total: (preds.results || []).length,
        created_at: preds.created_at
      }
    })
    .catch((e) => { error.value = '加载本地结果失败：' + String(e.message || e) })
    .finally(() => { loading.value = false })
}

async function loadLive () {
  if (!taskId.value.trim()) {
    run.value = null
    error.value = ''
    return
  }
  loading.value = true
  error.value = ''
  try {
    const job = await api.task(taskId.value.trim())
    run.value = {
      results: job.results || [],
      evaluation: job.evaluation || null,
      model: job.model,
      mode: job.mode,
      total: job.total,
      created_at: job.created_at,
      status: job.status,
      errors: job.errors || []
    }
    localStorage.setItem('lastTaskId', taskId.value.trim())
  } catch (e) {
    error.value = '加载任务失败：' + String(e.message || e)
    run.value = null
  } finally {
    loading.value = false
  }
}

watch(() => props.source, (s) => { if (s === 'local') loadLocal(); else loadLive() })
onMounted(() => { props.source === 'local' ? loadLocal() : loadLive() })

const verdictData = computed(() => {
  const counts = {}
  for (const r of results.value) counts[r.verdict] = (counts[r.verdict] || 0) + 1
  return Object.entries(counts).map(([k, v]) => ({
    name: verdictMeta(k).label,
 value: v,
    itemStyle: { color: verdictMeta(k).color }
  }))
})

const categoryData = computed(() => {
  const counts = {}
  for (const r of results.value) for (const t of r.types || []) counts[t] = (counts[t] || 0) + 1
  return Object.entries(CATEGORIES).map(([k, meta]) => ({ name: meta.label, value: counts[k] || 0, type: k }))
})

const severityData = computed(() => {
  const counts = { high: 0, medium: 0, low: 0 }
  for (const r of results.value) if (r.severity) counts[r.severity] = (counts[r.severity] || 0) + 1
  return [
    { name: '高', value: counts.high, itemStyle: { color: SEVERITIES.high.color } },
    { name: '中', value: counts.medium, itemStyle: { color: SEVERITIES.medium.color } },
    { name: '低', value: counts.low, itemStyle: { color: SEVERITIES.low.color } }
  ]
})

const pieOption = computed(() => ({
  tooltip: { trigger: 'item', formatter: '{b}: {c} 条 ({d}%)' },
  legend: { bottom: 0, icon: 'circle', textStyle: { fontSize: 12, color: '#475569' } },
  series: [{
    type: 'pie', radius: ['45%', '70%'], avoidLabelOverlap: true,
    label: { show: true, formatter: '{b}\n{c} 条', fontSize: 12 },
    data: verdictData.value
  }]
}))

function barOption (data, title) {
  const items = data.value.filter((d) => d.value > 0)
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 8, right: 16, top: 8, bottom: 56, containLabel: true },
    xAxis: {
      type: 'category', data: items.map((d) => d.name),
      axisLabel: { interval: 0, fontSize: 11, color: '#475569', rotate: items.length > 4 ? 24 : 0 }
    },
    yAxis: { type: 'value', minInterval: 1, axisLabel: { fontSize: 11, color: '#94a3b8' }, splitLine: { lineStyle: { color: '#e2e8f0' } } },
    series: [{
      type: 'bar', data: items.map((d) => d.itemStyle ? d : { ...d, itemStyle: { color: '#2563eb' } }),
      barMaxWidth: 44,
      label: { show: true, position: 'top', fontSize: 12, color: '#0f172a' }
    }]
  }
}
</script>

<template>
  <div class="dash">
    <div v-if="source === 'live'" class="card toolbar">
      <div class="field" style="flex: 1">
        <label>任务 ID</label>
        <div style="display: flex; gap: 8px">
          <input class="input" v-model="taskId" placeholder="提交检测后获得的 UUID" @keyup.enter="loadLive" style="flex: 1" />
          <button class="btn primary" @click="loadLive" :disabled="loading">加载</button>
        </div>
      </div>
    </div>

    <div v-if="loading" class="card empty">加载中…</div>
    <div v-else-if="error" class="card empty" style="color: var(--danger)">{{ error }}</div>

    <template v-else-if="run">
      <div class="card run-info">
        <div class="info-item">
          <span class="info-label">模型</span>
          <span class="mono">{{ run.model || '—' }}</span>
        </div>
        <div class="info-item">
          <span class="info-label">模式</span>
          <span class="badge" :style="{ background: run.mode === 'llm' ? 'var(--primary-soft)' : 'var(--muted-soft)', color: run.mode === 'llm' ? 'var(--primary)' : 'var(--muted)' }">
            {{ run.mode === 'llm' ? '真实 LLM' : 'mock 演示' }}
          </span>
        </div>
        <div class="info-item">
          <span class="info-label">样本数</span>
          <span>{{ run.total }}</span>
        </div>
        <div class="info-item" v-if="evaluation">
          <span class="info-label">评估状态</span>
          <span>{{ evaluation.status === 'evaluated' ? '已评估' : evaluation.status === 'simulation_only' ? '模拟，无正式指标' : evaluation.status }}</span>
        </div>
        <div class="info-item" v-if="evaluation?.false_positive_ids?.length" style="color: var(--danger)">
          <span class="info-label">误报</span>
          <span class="mono">{{ evaluation.false_positive_ids.join('、') }}</span>
        </div>
        <div class="info-item" v-if="evaluation?.false_negative_ids?.length" style="color: var(--warn)">
          <span class="info-label">漏检</span>
          <span class="mono">{{ evaluation.false_negative_ids.join('、') }}</span>
        </div>
      </div>

      <MetricCards :metrics="metrics" :total="run.total" />

      <div class="charts">
        <div class="card chart-card">
          <h3 class="chart-title">判断分布</h3>
          <v-chart class="chart" :option="pieOption" autoresize />
        </div>
        <div class="card chart-card">
          <h3 class="chart-title">幻觉类型分布</h3>
          <v-chart class="chart" :option="barOption(categoryData)" autoresize />
          <div class="chart-note">支持多标签，条数总和可大于样本数</div>
        </div>
        <div class="card chart-card">
          <h3 class="chart-title">严重程度分布</h3>
          <v-chart class="chart" :option="barOption(severityData)" autoresize />
        </div>
      </div>
    </template>

    <div v-else class="card empty">
      {{ source === 'live' ? '输入任务 ID 加载检测结果，或先在「提交检测」页发起一次检测。' : '暂无数据。' }}
    </div>
  </div>
</template>

<style scoped>
.dash { display: flex; flex-direction: column; gap: 14px; }
.toolbar { padding: 14px 16px; }
.run-info { display: flex; flex-wrap: wrap; gap: 28px; padding: 14px 18px; }
.info-item { display: flex; flex-direction: column; gap: 2px; }
.info-label { font-size: 11px; color: var(--text-3); font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; }
.charts { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
.chart-card { padding: 16px; }
.chart-title { font-size: 14px; margin-bottom: 8px; }
.chart { height: 300px; }
.chart-note { font-size: 11px; color: var(--text-3); margin-top: 6px; }
@media (max-width: 1000px) { .charts { grid-template-columns: 1fr; } }
</style>
