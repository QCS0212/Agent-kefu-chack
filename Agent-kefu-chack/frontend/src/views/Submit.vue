<script setup>
import { computed, onBeforeUnmount, ref } from 'vue'
import { api } from '../api.js'

const props = defineProps({ health: { type: Object, default: null } })
const emit = defineEmits(['submitted'])

const SAMPLE = [
  {
    id: 'case-001',
    user_question: '可以货到付款吗？',
    system_reply: '目前不支持货到付款，支持微信、支付宝和银行卡在线支付。',
    knowledge_base: '支付方式：微信支付、支付宝、银行卡、花呗、信用卡。不支持货到付款。'
  }
]

const TERMINAL = ['completed', 'partial_failed', 'failed', 'interrupted']

const mode = ref('mock')
const payload = ref(JSON.stringify(SAMPLE, null, 2))
const submitting = ref(false)
const error = ref('')
const taskId = ref('')
const job = ref(null)
const loadingSamples = ref(false)
const labels = ref('')
const evaluateError = ref('')
const evaluating = ref(false)
let timer = null

const reachable = computed(() => props.health !== null && props.health !== undefined)
const progress = computed(() => (job.value && job.value.progress) || null)
const finished = computed(() => Boolean(job.value) && TERMINAL.indexOf(job.value.status) >= 0)
const reportUrl = computed(() => (taskId.value ? api.reportUrl(taskId.value) : ''))

function stopPolling () {
  if (timer) { clearTimeout(timer); timer = null }
}
onBeforeUnmount(stopPolling)

function parseItems () {
  let value
  try {
    value = JSON.parse(payload.value)
  } catch (e) {
    throw new Error('JSON 解析失败：' + e.message)
  }
  const items = Array.isArray(value) ? value : value.items
  if (!Array.isArray(items) || !items.length) throw new Error('需要 items 数组，且至少 1 条')
  if (items.length > 20) throw new Error('单次最多 20 条，当前 ' + items.length + ' 条')
  return items
}

async function loadSamples () {
  loadingSamples.value = true
  error.value = ''
  try {
    const rows = await fetch('/data/replies.json').then((r) => r.json())
    payload.value = JSON.stringify(
      rows.map(({ id, user_question, system_reply, knowledge_base }) => ({ id, user_question, system_reply, knowledge_base })),
      null,
      2
    )
  } catch (e) {
    error.value = '载入 20 条样本失败：' + String(e.message || e)
  } finally {
    loadingSamples.value = false
  }
}

function useTemplate () {
  payload.value = JSON.stringify(SAMPLE, null, 2)
  error.value = ''
}

async function poll () {
  stopPolling()
  if (!taskId.value) return
  try {
    job.value = await api.task(taskId.value)
    localStorage.setItem('lastTaskId', taskId.value)
    if (!TERMINAL.includes(job.value.status)) timer = setTimeout(poll, 2000)
  } catch (e) {
    error.value = '查询任务失败：' + String(e.message || e)
  }
}

async function submit () {
  error.value = ''
  evaluateError.value = ''
  let items
  try {
    items = parseItems()
  } catch (e) {
    error.value = String(e.message || e)
    return
  }
  submitting.value = true
  try {
    const res = await api.submit(mode.value, items)
    taskId.value = res.task_id
    job.value = { status: 'queued', total: items.length, completed: 0, failed: 0, results: [], errors: [] }
    labels.value = JSON.stringify(items.map((i) => ({ id: i.id, is_hallucination: false })), null, 2)
    emit('submitted', res.task_id)
    timer = setTimeout(poll, 1200)
  } catch (e) {
    error.value = '提交失败：' + String(e.message || e)
  } finally {
    submitting.value = false
  }
}

async function evaluate () {
  evaluateError.value = ''
  let parsed
  try {
    parsed = JSON.parse(labels.value)
  } catch (e) {
    evaluateError.value = 'JSON 解析失败：' + e.message
    return
  }
  evaluating.value = true
  try {
    await api.evaluate(taskId.value, Array.isArray(parsed) ? parsed : parsed.labels)
    await poll()
  } catch (e) {
    evaluateError.value = '评估失败：' + String(e.message || e)
  } finally {
    evaluating.value = false
  }
}

function statusLabel (s) {
  return {
    queued: '排队中', running: '检测中', completed: '已完成',
    partial_failed: '部分失败', failed: '失败', interrupted: '已中断'
  }[s] || s
}

function statusTone (s) {
  if (s === 'completed') return { background: 'var(--ok-soft)', color: 'var(--ok)', borderColor: '#bbf7d0' }
  if (s === 'failed') return { background: 'var(--danger-soft)', color: 'var(--danger)', borderColor: '#fecaca' }
  if (s === 'partial_failed' || s === 'interrupted') return { background: 'var(--warn-soft)', color: 'var(--warn)', borderColor: '#fde68a' }
  return { background: 'var(--primary-soft)', color: 'var(--primary)', borderColor: '#bfdbfe' }
}
</script>

<template>
  <div class="submit">
    <div class="card panel">
      <div class="panel-head">
        <h3>提交检测</h3>
        <span class="badge" :style="statusTone(mode === 'llm' && !reachable ? 'failed' : 'completed')">
          {{ mode === 'llm' ? (reachable ? '真实 LLM 模式' : '接口不可用') : 'mock 演示模式' }}
        </span>
      </div>

      <div class="mode-row">
        <button class="btn" :class="{ primary: mode === 'mock' }" @click="mode = 'mock'">mock 演示</button>
        <button class="btn" :class="{ primary: mode === 'llm' }" @click="mode = 'llm'">真实 LLM</button>
        <span class="hint">
          mock 只验证工程链路，不产生正式检出率；真实模式使用服务端 .env 配置，客户端不传密钥。
        </span>
      </div>

      <div class="field">
        <label>检测数据（items 数组，1–20 条）</label>
        <textarea class="textarea" v-model="payload" spellcheck="false"></textarea>
      </div>

      <div class="actions">
        <button class="btn primary" :disabled="submitting" @click="submit">
          {{ submitting ? '提交中…' : '提交检测' }}
        </button>
        <button class="btn" :disabled="loadingSamples" @click="loadSamples">载入 20 条样本</button>
        <button class="btn" @click="useTemplate">恢复示例</button>
      </div>

      <div v-if="error" class="alert">{{ error }}</div>
    </div>

    <div class="card panel">
      <div class="panel-head">
        <h3>任务状态</h3>
        <span v-if="job" class="badge" :style="statusTone(job.status)">{{ statusLabel(job.status) }}</span>
      </div>

      <div v-if="!taskId" class="empty">尚未提交任务。提交后可在此查看进度、结果与报告。</div>

      <template v-else>
        <div class="kv">
          <span class="k">任务 ID</span>
          <span class="v mono">{{ taskId }}</span>
        </div>
        <div class="kv">
          <span class="k">进度</span>
          <span class="v">
            {{ progress ? progress.processed + ' / ' + progress.total : '—' }}
            <span v-if="job && job.failed">（失败 {{ job.failed }}）</span>
          </span>
        </div>
        <div class="kv" v-if="job && job.model">
          <span class="k">模型</span>
          <span class="v mono">{{ job.model }}</span>
        </div>

        <div class="actions">
          <button class="btn" @click="poll" :disabled="!taskId">刷新状态</button>
          <a v-if="finished" class="btn" :href="reportUrl" target="_blank" rel="noopener">打开 HTML 报告</a>
        </div>

        <div v-if="job && job.errors && job.errors.length" class="alert">
          技术失败 {{ job.errors.length }} 条：{{ job.errors.map((e) => e.id).join('、') }}（不计入正常或证据不足）
        </div>

        <div class="field" v-if="finished">
          <label>人工标签（独立评估，不发送给模型）</label>
          <textarea class="textarea small" v-model="labels" spellcheck="false"></textarea>
          <div class="actions">
            <button class="btn primary" :disabled="evaluating" @click="evaluate">
              {{ evaluating ? '评估中…' : '提交标签并计算指标' }}
            </button>
          </div>
          <div v-if="evaluateError" class="alert">{{ evaluateError }}</div>
          <div v-else-if="job.evaluation && job.evaluation.metrics" class="metrics">
            TP {{ job.evaluation.metrics.tp }} · FP {{ job.evaluation.metrics.fp }} · FN {{ job.evaluation.metrics.fn }} · TN {{ job.evaluation.metrics.tn }}
          </div>
          <div v-else-if="job.evaluation" class="hint">{{ job.evaluation.note }}</div>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.submit { display: flex; flex-direction: column; gap: 14px; }
.panel { padding: 16px; }
.panel-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.panel-head h3 { font-size: 15px; }
.mode-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
.hint { font-size: 12px; color: var(--text-3); }
.actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
.textarea { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; min-height: 220px; }
.textarea.small { min-height: 120px; }
.alert {
  margin-top: 12px; padding: 9px 12px; border-radius: 8px; font-size: 13px;
  background: var(--danger-soft); color: var(--danger); border: 1px solid #fecaca;
}
.kv { display: flex; gap: 10px; padding: 5px 0; border-bottom: 1px dashed var(--border); }
.kv:last-of-type { border-bottom: none; }
.k { width: 72px; color: var(--text-3); font-size: 12px; }
.v { flex: 1; word-break: break-all; }
.metrics { margin-top: 10px; font-size: 13px; color: var(--text); }
</style>