export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000'

const SAFE_ERRORS = {
  403: 'This browser is not allowed to control the local engine.',
  409: 'The engine is already running, changing state, or owned by another safe process.',
  422: 'The request could not be validated.',
  503: 'The backend could not complete this operation. Check its local configuration and logs.',
}

export class ApiError extends Error {
  constructor(status = 0) {
    super(status === 0
      ? 'The local API is unavailable. Check that the backend is running.'
      : SAFE_ERRORS[status] || 'The API request could not be completed.')
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
  if (!response.ok) throw new ApiError(response.status)
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
export const getLogs = (limit = 20) => request(`/api/logs?limit=${limit}`)
