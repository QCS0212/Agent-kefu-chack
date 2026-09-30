<script setup>
defineProps({
  metrics: { type: Object, default: null },
  total: { type: Number, default: 0 }
})

const pct = (v) => (v == null ? '—' : (v * 100).toFixed(2) + '%')
</script>

<template>
  <div v-if="metrics" class="grid">
    <div class="mcard card" v-for="item in [
      { label: '真正例 TP', value: metrics.tp, note: '正确检出的幻觉', tone: 'ok' },
      { label: '假正例 FP', value: metrics.fp, note: '误报：正常被判幻觉', tone: 'danger' },
      { label: '假负例 FN', value: metrics.fn, note: '漏检：幻觉被判正常', tone: 'warn' },
      { label: '真负例 TN', value: metrics.tn, note: '正确放行的正常', tone: 'ok' }
    ]" :key="item.label" :class="item.tone">
      <div class="m-label">{{ item.label }}</div>
      <div class="m-value">{{ item.value }}</div>
      <div class="m-note">{{ item.note }}</div>
    </div>
    <div class="mcard card accent" v-for="item in [
      { label: '精确率 Precision', value: pct(metrics.precision) },
      { label: '检出率 Recall', value: pct(metrics.recall) },
      { label: 'F1', value: pct(metrics.f1) },
      { label: '准确率 Accuracy', value: pct(metrics.accuracy) }
    ]" :key="item.label">
      <div class="m-label">{{ item.label }}</div>
      <div class="m-value big">{{ item.value }}</div>
      <div class="m-note">样本共 {{ total }} 条</div>
    </div>
  </div>
  <div v-else class="card empty">
    本次运行没有评估指标。提交人工标签后才会计算 Precision / Recall / F1 / Accuracy。
  </div>
</template>

<style scoped>
.grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.mcard { padding: 14px 16px; border-left: 3px solid var(--muted); }
.mcard.ok { border-left-color: var(--ok); }
.mcard.danger { border-left-color: var(--danger); }
.mcard.warn { border-left-color: var(--warn); }
.mcard.accent { border-left-color: var(--primary); }
.m-label { font-size: 12px; color: var(--text-2); font-weight: 600; }
.m-value { font-size: 26px; font-weight: 700; line-height: 1.2; margin-top: 2px; }
.m-value.big { font-size: 22px; color: var(--primary); }
.m-note { font-size: 11px; color: var(--text-3); margin-top: 2px; }
@media (max-width: 900px) { .grid { grid-template-columns: repeat(2, 1fr); } }
</style>
