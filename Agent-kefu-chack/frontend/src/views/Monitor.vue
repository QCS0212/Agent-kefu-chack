<script setup>
import { computed, onMounted, ref } from 'vue'
import { monitorApi, monitorToken, setMonitorToken } from '../api.js'
import { verdictMeta, severityMeta, categoryMeta } from '../constants.js'
import VChart from '../use-echarts.js'

const days = ref(7)
const stats = ref(null)
const series = ref([])
const events = ref([])
const loading = ref(false)
const error = ref('')
const onlyReview = ref(true)
const busyId = ref('')
const token = ref(monitorToken())

async function load () {
  loading.value = true
  error.value = ''
  try {
    const [s, t, e] = await Promise.all([
      monitorApi.stats(days.value),
      monitorApi.timeseries(days.value),
      monitorApi.events({ limit: 50, needs_review: onlyReview.value ? true : '', review_status: onlyReview.value ? 'pending' : '' })
    ])
    stats.value = s
    series.value = t
    events.value = e.items || []
  } catch (e) {
    error.value = '读取监测数据失败：' + String(e.message || e) + '（需要先启动 python -m monitor）'
    stats.value = null
    series.value = []
    events.value = []
  } finally {
    loading.value = false
  }
}

onMounted(load)

function applyToken () {
  setMonitorToken(token.value)
  load()
}

function pct (value) {
  return value === null || value === undefined ? '—' : (value * 100).toFixed(2) + '%'
}

const cards = computed(() => {
  const s = stats.value
  if (!s) return []
  return [
    { label: '幻觉率（明确结论口径）', value: pct(s.hallucination_rate), note: `明确结论 ${s.detections.decisive} / 已检测 ${s.detections.total}`, tone: 'accent' },
    { label: '覆盖率', value: pct(s.coverage), note: '不可核验不计入幻觉率分母', tone: 'ok' },
    { label: '降级率', value: pct(s.degraded_rate), note: `降级 ${s.detections.degraded} 条（上游失败 ${s.detections.fallback}）`, tone: s.detections.fallback ? 'warn' : 'ok' },
    { label: '复核后精确率', value: pct(s.review.precision_after_review), note: `确认 ${s.review.confirmed} / 误报 ${s.review.rejected}`, tone: 'accent' },
    { label: '待复核', value: s.review.pending + ' 条', note: '规则快检与降级结论需人工确认', tone: s.review.pending ? 'warn' : 'ok' },
    { label: '接入事件', value: s.events.total + ' 条', note: `抽样 ${s.events.sampled} 条`, tone: 'ok' }
  ]
})

const trendOption = computed(() => {
  const buckets = series.value.map((row) => row.bucket)
  const pick = (key) => series.value.map((row) => row[key] || 0)
  return {
    tooltip: { trigger: 'axis' },
    legend: { bottom: 0, icon: 'circle', textStyle: { fontSize: 12, color: '#475569' } },
    grid: { left: 8, right: 16, top: 12, bottom: 44, containLabel: true },
    xAxis: { type: 'category', data: buckets, axisLabel: { fontSize: 11, color: '#475569' } },
    yAxis: { type: 'value', minInterval: 1, splitLine: { lineStyle: { color: '#e2e8f0' } } },
    series: [
      { name: '幻觉', type: 'bar', stack: 'v', data: pick('hallucination'), itemStyle: { color: '#dc2626' } },
      { name: '正常', type: 'bar', stack: 'v', data: pick('no_hallucination'), itemStyle: { color: '#16a34a' } },
      { name: '待核验', type: 'bar', stack: 'v', data: pick('not_verifiable'), itemStyle: { color: '#94a3b8' } }
    ]
  }
})

const typeOption = computed(() => {
  const byType = (stats.value && stats.value.detections.by_type) || {}
  const items = Object.entries(byType).filter(([, v]) => v > 0)
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 8, right: 16, top: 12, bottom: 56, containLabel: true },
    xAxis: {
      type: 'category',
      data: items.map(([key]) => categoryMeta(key).label),
      axisLabel: { interval: 0, fontSize: 11, color: '#475569', rotate: items.length > 3 ? 20 : 0 }
    },
    yAxis: { type: 'value', minInterval: 1, splitLine: { lineStyle: { color: '#e2e8f0' } } },
    series: [{ type: 'bar', data: items.map(([, v]) => v), barMaxWidth: 44, itemStyle: { color: '#2563eb' }, label: { show: true, position: 'top', fontSize: 12 } }]
  }
})

function shortId (id) {
  return (id || '').slice(0, 8)
}

async function review (event, status) {
  busyId.value = event.event_id
  try {
    await monitorApi.review(event.event_id, status)
    await load()
  } catch (e) {
    error.value = '提交复核失败：' + String(e.message || e)
  } finally {
    busyId.value = ''
  }
}
</script>

<template>
  <div class="monitor">
    <div class="card toolbar">
      <div class="field">
        <label>统计窗口</label>
        <select class="select" v-model.number="days" @change="load">
          <option :value="1">最近 1 天</option>
          <option :value="7">最近 7 天</option>
          <option :value="30">最近 30 天</option>
        </select>
      </div>
      <label class="check"><input type="checkbox" v-model="onlyReview" @change="load" /> 只看待复核</label>
      <div class="field">
        <label>面板令牌（配置 MONITOR_PANEL_TOKEN 时必填）</label>
        <div style="display: flex; gap: 8px">
          <input class="input" v-model="token" type="password" placeholder="X-Monitor-Token" @keyup.enter="applyToken" />
          <button class="btn" @click="applyToken">保存</button>
        </div>
      </div>
      <button class="btn" @click="load" :disabled="loading">{{ loading ? '刷新中…' : '刷新' }}</button>
      <span class="hint">数据来自监测平台（127.0.0.1:8010），需要先运行 <code>python -m monitor</code></span>
    </div>

    <div v-if="error" class="card empty" style="color: var(--danger)">{{ error }}</div>

    <template v-else-if="stats">
      <div class="cards">
        <div class="card mcard" v-for="card in cards" :key="card.label" :class="card.tone">
          <div class="m-label">{{ card.label }}</div>
          <div class="m-value">{{ card.value }}</div>
          <div class="m-note">{{ card.note }}</div>
        </div>
      </div>

      <div class="charts">
        <div class="card chart-card">
          <h3 class="chart-title">判断趋势</h3>
          <v-chart class="chart" :option="trendOption" autoresize />
        </div>
        <div class="card chart-card">
          <h3 class="chart-title">幻觉类型分布</h3>
          <v-chart class="chart" :option="typeOption" autoresize />
        </div>
      </div>

      <div class="card">
        <div class="panel-head">
          <h3>{{ onlyReview ? '待复核队列' : '最近事件' }}</h3>
          <span class="hint">共 {{ events.length }} 条</span>
        </div>
        <div v-if="!events.length" class="empty">暂无事件。</div>
        <table v-else class="table">
          <thead>
            <tr>
              <th style="width: 76px">事件</th>
              <th style="width: 110px">结论</th>
              <th style="width: 120px">类型 / 严重度</th>
              <th>理由</th>
              <th style="width: 150px">复核</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="event in events" :key="event.event_id">
              <td class="mono" :title="event.event_id">{{ shortId(event.event_id) }}</td>
              <td>
                <span v-if="event.detection" class="badge" :style="{ background: verdictMeta(event.detection.verdict).soft, color: verdictMeta(event.detection.verdict).color, borderColor: verdictMeta(event.detection.verdict).color + '33' }">
                  {{ verdictMeta(event.detection.verdict).label }}
                </span>
                <span v-else class="badge" style="background: var(--muted-soft); color: var(--muted)">{{ event.status }}</span>
                <div class="mono detector">{{ event.detection ? event.detection.detector : '—' }}</div>
              </td>
              <td>
                <span v-for="t in (event.detection ? event.detection.types : [])" :key="t" class="chip" :style="{ borderColor: categoryMeta(t).color + '55', color: categoryMeta(t).color }">
                  {{ categoryMeta(t).label }}
                </span>
                <div v-if="event.detection && event.detection.severity" class="mono" :style="{ color: severityMeta(event.detection.severity).color }">
                  严重度 {{ severityMeta(event.detection.severity).label }}
                </div>
              </td>
              <td class="reason">{{ event.detection ? event.detection.reason : (event.error || '—') }}</td>
              <td>
                <span v-if="event.review_status !== 'pending'" class="badge" :style="event.review_status === 'confirmed' ? { background: 'var(--ok-soft)', color: 'var(--ok)' } : { background: 'var(--warn-soft)', color: 'var(--warn)' }">
                  {{ event.review_status === 'confirmed' ? '已确认' : '误报' }}
                </span>
                <div class="row-actions" v-else>
                  <button class="btn btn-xs" :disabled="busyId === event.event_id" @click="review(event, 'confirmed')">确认</button>
                  <button class="btn btn-xs" :disabled="busyId === event.event_id" @click="review(event, 'rejected')">误报</button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </div>
</template>

<style scoped>
.monitor { display: flex; flex-direction: column; gap: 14px; }
.toolbar { display: flex; align-items: flex-end; gap: 12px; flex-wrap: wrap; padding: 14px 16px; }
.check { display: inline-flex; align-items: center; gap: 5px; font-size: 13px; color: var(--text-2); }
.hint { font-size: 12px; color: var(--text-3); margin-left: auto; }
.hint code { background: var(--surface-2); border: 1px solid var(--border); border-radius: 4px; padding: 0 4px; }
.cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.mcard { padding: 14px 16px; border-left: 3px solid var(--muted); }
.mcard.ok { border-left-color: var(--ok); }
.mcard.warn { border-left-color: var(--warn); }
.mcard.accent { border-left-color: var(--primary); }
.m-label { font-size: 12px; color: var(--text-2); font-weight: 600; }
.m-value { font-size: 24px; font-weight: 700; line-height: 1.2; margin-top: 2px; }
.m-note { font-size: 11px; color: var(--text-3); margin-top: 2px; }
.charts { display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px; }
.chart-card { padding: 16px; }
.chart-title { font-size: 14px; margin-bottom: 8px; }
.chart { height: 280px; }
.panel-head { display: flex; align-items: center; justify-content: space-between; padding: 14px 16px; }
.table { width: 100%; border-collapse: collapse; font-size: 13px; }
.table th { text-align: left; padding: 8px 12px; font-size: 11px; color: var(--text-3); border-bottom: 1px solid var(--border); font-weight: 600; }
.table td { padding: 10px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }
.detector { color: var(--text-3); margin-top: 2px; }
.reason { color: var(--text-2); max-width: 460px; }
.row-actions { display: flex; gap: 6px; }
.btn-xs { padding: 3px 10px; font-size: 12px; }
@media (max-width: 1000px) {
  .cards, .charts { grid-template-columns: 1fr; }
  .hint { margin-left: 0; }
}
</style>