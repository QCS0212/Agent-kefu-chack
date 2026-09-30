const BASE = ''

async function request (path, options = {}) {
  const { headers, ...rest } = options
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json; charset=utf-8', ...(headers || {}) },
    ...rest
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

// 监测平台的接入/面板令牌：存在 localStorage，供「线上监测」页签使用
export function monitorToken () {
  return localStorage.getItem('monitorToken') || ''
}

export function setMonitorToken (value) {
  const token = (value || '').trim()
  if (token) localStorage.setItem('monitorToken', token)
  else localStorage.removeItem('monitorToken')
}

function monitorHeaders () {
  const token = monitorToken()
  return token ? { 'X-Monitor-Token': token } : {}
}

// 批量检测 API 的接入 Key（企业形态下调 /api/checks* 需要）
export function apiKey () {
  return localStorage.getItem('apiKey') || ''
}

export function setApiKey (value) {
  const key = (value || '').trim()
  if (key) localStorage.setItem('apiKey', key)
  else localStorage.removeItem('apiKey')
}

function keyHeaders () {
  const key = apiKey()
  return key ? { 'X-Api-Key': key } : {}
}

export const api = {
  health: () => request('/health'),
  submit: (mode, items) => request('/api/checks', { method: 'POST', headers: keyHeaders(), body: JSON.stringify({ mode, items }) }),
  task: (id) => request(`/api/checks/${id}`, { headers: keyHeaders() }),
  evaluate: (id, labels) => request(`/api/checks/${id}/evaluate`, { method: 'POST', headers: keyHeaders(), body: JSON.stringify({ labels }) }),
  reportUrl: (id) => `/api/checks/${id}/report`
}

export const monitorApi = {
  stats: (days) => request('/api/monitor/stats' + query({ days }), { headers: monitorHeaders() }),
  timeseries: (days = 7) => request('/api/monitor/timeseries' + query({ days }), { headers: monitorHeaders() }),
  events: (params = {}) =>
    request('/api/monitor/events' + query({ limit: 50, ...params }), { headers: monitorHeaders() }),
  alerts: (params = {}) =>
    request('/api/monitor/alerts' + query({ limit: 50, ...params }), { headers: monitorHeaders() }),
  review: (id, status, note) =>
    request(`/api/monitor/events/${id}/review`, {
      method: 'POST',
      headers: monitorHeaders(),
      body: JSON.stringify({ status, note: note || null })
    }),
  replayAlert: (id) =>
    request(`/api/monitor/alerts/${id}/replay`, { method: 'POST', headers: monitorHeaders() }),
  config: () => request('/api/monitor/config', { headers: monitorHeaders() })
}