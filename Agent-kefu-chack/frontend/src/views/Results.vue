<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { api } from '../api.js'
import { CATEGORIES, verdictMeta, severityMeta, categoryMeta, relationMeta } from '../constants.js'

const props = defineProps({ source: { type: String, default: 'local' } })

const loading = ref(false)
const error = ref('')
const results = ref([])
const sources = ref({})
const labels = ref({}) // id -> is_hallucination
const fpIds = ref(new Set())
const fnIds = ref(new Set())
const expanded = ref(null)

const q = ref('')
const verdict = ref('')
const category = ref('')
const severity = ref('')
const onlyMismatch = ref(false)

function loadLocal () {
  loading.value = true
  error.value = ''
  Promise.all([
    fetch('/data/predictions.json').then((r) => r.json()),
    fetch('/data/replies.json').then((r) => r.json()),
    fetch('/data/ground_truth.json').then((r) => r.json()),
    fetch('/data/metrics.json').then((r) => r.json())
  ])
    .then(([preds, reps, truth, ev]) => {
      results.value = preds.results || []
      sources.value = {}
      for (const r of reps) sources.value[r.id] = r
      labels.value = {}
      for (const t of truth) labels.value[t.id] = t.is_hallucination
      fpIds.value = new Set(ev.false_positive_ids || [])
      fnIds.value = new Set(ev.false_negative_ids || [])
    })
    .catch((e) => { error.value = '加载本地结果失败：' + String(e.message || e) })
    .finally(() => { loading.value = false })
}

async function loadLive (id) {
  const taskId = id || localStorage.getItem('lastTaskId')
  if (!taskId) { results.value = []; return }
  loading.value = true
  error.value = ''
  try {
    const job = await api.task(taskId)
    results.value = job.results || []
    sources.value = {}
    for (const it of job.items || []) sources.value[it.id] = it
    fpIds.value = new Set(job.evaluation?.false_positive_ids || [])
    fnIds.value = new Set(job.evaluation?.false_negative_ids || [])
    labels.value = {}
    // 有人工评估结果时可还原每条标签
    const detected = new Set(results.value.filter((r) => r.verdict === 'hallucination').map((r) => r.id))
    for (const r of results.value) {
      const id = r.id
      if (fpIds.value.has(id)) labels.value[id] = false
      else if (fnIds.value.has(id)) labels.value[id] = true
      else if (detected.has(id)) labels.value[id] = true
      else labels.value[id] = false
    }
  } catch (e) {
    error.value = '加载任务失败：' + String(e.message || e)
    results.value = []
  } finally {
    loading.value = false
  }
}

watch(() => props.source, (s) => { if (s === 'local') loadLocal(); else loadLive() })
onMounted(() => { props.source === 'local' ? loadLocal() : loadLive() })

function matchOf (r) {
  if (!(r.id in labels.value)) return null
  const detected = r.verdict === 'hallucination'
  const label = labels.value[r.id]
  if (detected && label) return 'tp'
  if (detected && !label) return 'fp'
  if (!detected && label) return 'fn'
  return 'tn'
}

const MATCH_META = {
  tp: { label: '正确检出', color: '#16a34a', soft: '#f0fdf4' },
  fp: { label: '误报', color: '#dc2626', soft: '#fef2f2' },
  fn: { label: '漏检', color: '#d97706', soft: '#fffbeb' },
  tn: { label: '正确放行', color: '#64748b', soft: '#f1f5f9' }
}

const filtered = computed(() => {
  const text = q.value.trim().toLowerCase()
  return results.value.filter((r) => {
    const src = sources.value[r.id] || {}
    if (verdict.value && r.verdict !== verdict.value) return false
    if (category.value && !(r.types || []).includes(category.value)) return false
    if (severity.value && r.severity !== severity.value) return false
    if (onlyMismatch.value) {
      const m = matchOf(r)
      if (m !== 'fp' && m !== 'fn') return false
    }
    if (text) {
      const hay = [r.id, r.reason, src.user_question, src.system_reply, src.knowledge_base, (r.types || []).join(' ')].join(' ').toLowerCase()
      if (!hay.includes(text)) return false
    }
    return true
  })
})

function exportJson () {
  const blob = new Blob([JSON.stringify({ results: filtered.value }, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = 'detection-results.json'
  a.click()
  URL.revokeObjectURL(url)
}
</script>

<template>
  <div class="results">
    <div class="card filters">
      <div class="search-wrap">
        <svg class="search-icon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" stroke-width="2" stroke-linecap="round">
          <circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>
        </svg>
        <input class="input search" v-model="q" placeholder="搜索 ID、回复内容、理由或分类…" />
      </div>
      <select class="select" v-model="verdict">
        <option value="">全部判断</option>
        <option value="hallucination">幻觉</option>
        <option value="no_hallucination">正常</option>
        <option value="not_verifiable">待核验</option>
      </select>
      <select class="select" v-model="category">
        <option value="">全部类型</option>
        <option v-for="(meta, key) in CATEGORIES" :key="key" :value="key">{{ meta.label }}</option>
      </select>
      <select class="select" v-model="severity">
        <option value="">全部严重程度</option>
        <option value="high">高</option>
        <option value="medium">中</option>
        <option value="low">低</option>
      </select>
      <label class="check">
        <input type="checkbox" v-model="onlyMismatch" /> 只看误报/漏检
      </label>
      <button class="btn" @click="exportJson" :disabled="!filtered.length">导出 JSON</button>
    </div>

    <div class="count-line">
      共 {{ results.length }} 条 · 当前显示 {{ filtered.length }} 条
      <span v-if="onlyMismatch" class="badge" style="margin-left: 8px; background: var(--warn-soft); color: var(--warn); border-color: #fde68a">仅误报/漏检</span>
    </div>

    <div v-if="loading" class="card empty">加载中…</div>
    <div v-else-if="error" class="card empty" style="color: var(--danger)">{{ error }}</div>
    <div v-else-if="!filtered.length" class="card empty">没有符合筛选条件的记录</div>

    <div v-else class="list">
      <div v-for="r in filtered" :key="r.id" class="card item" :class="{ open: expanded === r.id }">
        <div class="item-head" @click="expanded = expanded === r.id ? null : r.id">
          <span class="id-chip mono">{{ r.id }}</span>
          <span class="badge" :style="{ background: verdictMeta(r.verdict).soft, color: verdictMeta(r.verdict).color, borderColor: verdictMeta(r.verdict).color + '33' }">
            {{ verdictMeta(r.verdict).label }}
          </span>
          <span v-for="t in r.types" :key="t" class="chip" :style="{ borderColor: categoryMeta(t).color + '55', color: categoryMeta(t).color }">
            {{ categoryMeta(t).label }}
          </span>
          <span v-if="r.severity" class="badge" :style="{ background: severityMeta(r.severity).soft, color: severityMeta(r.severity).color, borderColor: severityMeta(r.severity).color + '33' }">
            严重度 · {{ severityMeta(r.severity).label }}
          </span>
          <span v-if="matchOf(r)" class="badge" :style="{ background: MATCH_META[matchOf(r)].soft, color: MATCH_META[matchOf(r)].color, borderColor: MATCH_META[matchOf(r)].color + '33' }">
            {{ MATCH_META[matchOf(r)].label }}
          </span>
          <span class="spacer"></span>
          <span class="reason">{{ r.reason }}</span>
          <svg class="caret" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" stroke-width="2.5" stroke-linecap="round" :class="{ flip: expanded === r.id }">
            <path d="m6 9 6 6 6-6"/>
          </svg>
        </div>
        <div v-if="expanded === r.id" class="item-body">
          <div class="source-grid">
            <div class="src-block">
              <div class="src-label">用户问题</div>
              <div class="src-text">{{ (sources[r.id] || {}).user_question || '—' }}</div>
            </div>
            <div class="src-block">
              <div class="src-label">客服回复</div>
              <div class="src-text">{{ (sources[r.id] || {}).system_reply || '—' }}</div>
            </div>
            <div class="src-block">
              <div class="src-label">知识库依据</div>
              <div class="src-text kb">{{ (sources[r.id] || {}).knowledge_base || '—' }}</div>
            </div>
          </div>
          <div class="claims-title">逐条事实核验（{{ (r.claims || []).length }} 条声明）</div>
          <div v-if="r.claims && r.claims.length" class="claims scrollable">
            <table class="claims-table">
              <thead>
                <tr>
                  <th style="width: 96px">关系</th>
                  <th>回复原文</th>
                  <th>知识库原文</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(c, i) in r.claims" :key="i">
                  <td>
                    <span class="badge" :style="{ background: relationMeta(c.relation).soft, color: relationMeta(c.relation).color, borderColor: relationMeta(c.relation).color + '33' }">
                      {{ relationMeta(c.relation).label }}
                    </span>
                  </td>
                  <td><div class="claim-quote">{{ c.reply_quote || '—' }}</div></td>
                  <td><div class="claim-quote" :class="{ empty: !c.knowledge_quote }">{{ c.knowledge_quote || '（无对应知识依据）' }}</div></td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-else class="empty" style="padding: 16px">本次判定未输出逐条声明</div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.results { display: flex; flex-direction: column; gap: 12px; }
.filters { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 12px 14px; }
.search-wrap { position: relative; flex: 1; min-width: 220px; }
.search-icon { position: absolute; left: 10px; top: 50%; transform: translateY(-50%); }
.search { width: 100%; padding-left: 32px; }
.check { display: inline-flex; align-items: center; gap: 5px; font-size: 13px; color: var(--text-2); white-space: nowrap; }
.count-line { font-size: 12px; color: var(--text-2); padding: 0 4px; }
.list { display: flex; flex-direction: column; gap: 8px; }
.item { transition: box-shadow 0.15s; }
.item.open { box-shadow: 0 2px 8px rgba(37, 99, 235, 0.08), 0 8px 24px rgba(15, 23, 42, 0.08); border-color: #bfdbfe; }
.item-head { display: flex; align-items: center; gap: 8px; padding: 12px 14px; cursor: pointer; flex-wrap: wrap; }
.item-head:hover { background: var(--surface-2); border-radius: 10px 10px 0 0; }
.id-chip { font-weight: 700; color: var(--text); background: var(--surface-2); border: 1px solid var(--border); padding: 1px 8px; border-radius: 6px; }
.spacer { flex: 1; }
.reason { font-size: 12px; color: var(--text-2); max-width: 46%; }
.caret { flex-shrink: 0; transition: transform 0.15s; }
.caret.flip { transform: rotate(180deg); }
.item-body { border-top: 1px solid var(--border); padding: 14px; display: flex; flex-direction: column; gap: 12px; }
.source-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.src-block { display: flex; flex-direction: column; gap: 4px; }
.src-label { font-size: 11px; font-weight: 700; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.04em; }
.src-text { font-size: 13px; color: var(--text); background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; }
.src-text.kb { color: #1e40af; background: #eff6ff; border-color: #bfdbfe; }
.claims-title { font-size: 12px; font-weight: 700; color: var(--text-2); }
.claims-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.claims-table th { text-align: left; padding: 6px 10px; color: var(--text-3); font-weight: 600; border-bottom: 1px solid var(--border); font-size: 11px; }
.claims-table td { padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
.claim-quote { background: var(--surface-2); border-radius: 6px; padding: 5px 8px; color: var(--text); }
.claim-quote.empty { color: var(--text-3); font-style: italic; }
@media (max-width: 900px) {
  .source-grid { grid-template-columns: 1fr; }
  .reason { max-width: 100%; }
}
</style>
