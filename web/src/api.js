const base = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '')

export async function api(path, options = {}) {
  const response = await fetch(base + path, { ...options, headers: { ...(options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }), ...options.headers } })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    const detail = body?.detail
    const error = new Error(Array.isArray(detail) ? detail.map(e => `${e.loc?.slice(1).join('.') || ''}: ${e.msg}`).join('\n') : typeof detail === 'string' ? detail : detail?.message || `服务请求失败 / Request failed (${response.status})`)
    error.status = response.status
    throw error
  }
  return response.status === 204 ? null : response.json()
}
export const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body) })
export async function downloadResearch(id, format) {
  const response = await fetch(`${base}/api/research-sessions/${id}/export?format=${format}`)
  if (!response.ok) throw new Error('导出失败 / Export failed')
  const url = URL.createObjectURL(await response.blob())
  const link = document.createElement('a')
  link.href = url; link.download = `${id}.${format}`; link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
export async function uploadFile(file, role) {
  if (file.size > 50 * 1024 * 1024) throw new Error('文件不能超过 50 MB / Maximum file size: 50 MB')
  const data = new FormData()
  data.append('file', file)
  data.append('role', role)
  return api('/api/files', { method: 'POST', body: data })
}
export function eventStream(runId, after) { return new EventSource(`${base}/api/runs/${runId}/events?after=${after}`) }
export function downloadJson(name, data) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url; link.download = name; link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
