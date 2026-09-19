export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000'

const SAFE_ERRORS = {
  400: 'The monitoring configuration is invalid. Check the folder and schedule.',
  403: 'This browser is not allowed to control the local engine.',
  409: 'The engine is already running, changing state, or owned by another safe process.',
  422: 'The request could not be validated.',
  404: 'The requested monitoring job was not found.',
  500: 'The backend could not complete this operation safely.',
  503: 'The backend could not complete this operation. Check its local configuration and logs.',
}

const SAFE_DETAILS = new Set([
  'Stop the engine before modifying monitoring configuration.',
  'A task or monitoring job already uses this ID.',
  'Monitoring job was not found.',
  'Folder path must identify an existing accessible directory.',
  'Monitoring configuration is invalid.',
  'Configuration could not be saved; the previous configuration was preserved.',
  'Stop the engine before changing email settings.',
  'An App Password is required.',
  'No App Password is configured.',
  'Email settings are invalid.',
  'Secure credential storage is unavailable.',
  'App Password could not be stored securely.',
  'Saved App Password could not be removed.',
  'SMTP authentication failed or the server could not be reached.',
  'Forget the saved App Password before changing sender email.',
])

export class ApiError extends Error {
  constructor(status = 0, detail = '') {
    super(status === 0
      ? 'The local API is unavailable. Check that the backend is running.'
      : SAFE_DETAILS.has(detail) ? detail : SAFE_ERRORS[status] || 'The API request could not be completed.')
    this.name = 'ApiError'
    this.status = status
  }
}

async function request(path, options = {}) {
  let response
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...options,
      headers: { Accept: 'application/json', ...options.headers },
    })
  } catch {
    throw new ApiError(0)
  }
  if (!response.ok) {
    let detail = ''
    try { detail = (await response.json()).detail || '' } catch { /* use fixed fallback */ }
    throw new ApiError(response.status, detail)
  }
  try {
    return await response.json()
  } catch {
    throw new ApiError(response.status)
  }
}

export const getHealth = () => request('/api/health')
export const getStatus = () => request('/api/status')
export const startEngine = () => request('/api/engine/start', { method: 'POST' })
export const stopEngine = () => request('/api/engine/stop', { method: 'POST' })
export const getTasks = () => request('/api/tasks')
export const getMonitoringJobs = () => request('/api/monitoring-jobs')
export const getMonitoringJob = (id) => request('/api/monitoring-jobs/' + encodeURIComponent(id))
export const createMonitoringJob = (body) => request('/api/monitoring-jobs', {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
})
export const updateMonitoringJob = (id, body) => request('/api/monitoring-jobs/' + encodeURIComponent(id), {
  method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
})
export const setMonitoringJobEnabled = (id, enabled) => request('/api/monitoring-jobs/' + encodeURIComponent(id) + '/enabled', {
  method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled }),
})
export const deleteMonitoringJob = (id) => request('/api/monitoring-jobs/' + encodeURIComponent(id), { method: 'DELETE' })
export const validateFolder = (path) => request('/api/folders/validate', {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path }),
})
export const getEmailSettings = () => request('/api/email-settings')
export const updateEmailSettings = (body) => request('/api/email-settings', {
  method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
})
export const sendTestEmail = () => request('/api/email-settings/test', { method: 'POST' })
export const forgetEmailCredential = () => request('/api/email-settings/credential', { method: 'DELETE' })
export const getLogs = (limit = 20) => request(`/api/logs?limit=${limit}`)
