const BASE = ''

async function request (path, options = {}) {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
    ...options
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = Array.isArray(body.detail)
        ? body.detail.map((e) => `${e.location.join('.')}: ${e.message}`).join('; ')
        : body.detail || detail
    } catch { /* 非 JSON 错误体 */ }
    throw new Error(`${res.status} ${detail}`)
  }
  return res.json()
}

function query (params) {
  const entries = Object.entries(params).filter(([, v]) => v !== '' && v !== null && v !== undefined)
  return entries.length ? '?' + new URLSearchParams(entries).toString() : ''
}

export const api = {
  health: () => request('/health'),
  submit: (mode, items) => request('/api/checks', { method: 'POST', body: JSON.stringify({ mode, items }) }),
  task: (id) => request(`/api/checks/${id}`),
  evaluate: (id, labels) => request(`/api/checks/${id}/evaluate`, { method: 'POST', body: JSON.stringify({ labels }) }),
  reportUrl: (id) => `/api/checks/${id}/report`
}

// 监测平台接口（vite 代理 /api/monitor* 到 127.0.0.1:8010）
export const monitorApi = {
  stats: (days) => request('/api/monitor/stats' + query({ days })),
  timeseries: (days = 7) => request('/api/monitor/timeseries' + query({ days })),
  events: (params = {}) => request('/api/monitor/events' + query({ limit: 50, ...params })),
  review: (id, status, note) =>
    request(`/api/monitor/events/${id}/review`, {
      method: 'POST',
      body: JSON.stringify({ status, note: note || null })
    })
}