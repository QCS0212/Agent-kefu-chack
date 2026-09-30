export const CATEGORIES = {
  policy_error: { label: '政策与优惠错误', color: '#dc2626' },
  product_error: { label: '产品事实错误', color: '#ea580c' },
  business_fabrication: { label: '业务信息编造', color: '#d97706' },
  capability_overreach: { label: '能力越界与虚假执行', color: '#7c3aed' },
  safety_misleading: { label: '安全提示失真', color: '#be123c' },
  misleading_omission: { label: '条件遗漏与过度概括', color: '#0891b2' }
}

export const VERDICTS = {
  hallucination: { label: '幻觉', color: '#dc2626', soft: '#fef2f2' },
  no_hallucination: { label: '正常', color: '#16a34a', soft: '#f0fdf4' },
  not_verifiable: { label: '待核验', color: '#64748b', soft: '#f1f5f9' }
}

export const SEVERITIES = {
  high: { label: '高', color: '#dc2626', soft: '#fef2f2' },
  medium: { label: '中', color: '#d97706', soft: '#fffbeb' },
  low: { label: '低', color: '#0284c7', soft: '#f0f9ff' }
}

export const RELATIONS = {
  supported: { label: '知识支持', color: '#16a34a', soft: '#f0fdf4' },
  contradicted: { label: '与知识冲突', color: '#dc2626', soft: '#fef2f2' },
  unsupported: { label: '无依据', color: '#d97706', soft: '#fffbeb' },
  not_verifiable: { label: '不可核验', color: '#64748b', soft: '#f1f5f9' }
}

export const TASK_STATUS = {
  queued: { label: '排队中', color: '#64748b', soft: '#f1f5f9' },
  running: { label: '检测中', color: '#2563eb', soft: '#eff6ff' },
  completed: { label: '已完成', color: '#16a34a', soft: '#f0fdf4' },
  partial_failed: { label: '部分失败', color: '#d97706', soft: '#fffbeb' },
  failed: { label: '失败', color: '#dc2626', soft: '#fef2f2' },
  interrupted: { label: '已中断', color: '#64748b', soft: '#f1f5f9' }
}

export const verdictMeta = (v) => VERDICTS[v] || { label: v, color: '#64748b', soft: '#f1f5f9' }
export const severityMeta = (s) => SEVERITIES[s] || { label: s || '未分级', color: '#94a3b8', soft: '#f8fafc' }
export const relationMeta = (r) => RELATIONS[r] || { label: r, color: '#64748b', soft: '#f1f5f9' }
export const categoryMeta = (c) => CATEGORIES[c] || { label: c, color: '#64748b' }
export const taskStatusMeta = (s) => TASK_STATUS[s] || { label: s, color: '#64748b', soft: '#f1f5f9' }
