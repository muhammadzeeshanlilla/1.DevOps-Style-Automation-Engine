const healthyStates = new Set(['RUNNING', 'alive', 'connected', 'SUCCESS'])
const pendingStates = new Set(['STARTING', 'STOPPING', 'REQUESTED', 'START_REQUESTED'])

export default function StatusBadge({ state, prefix = '' }) {
  const normalized = state || 'Unavailable'
  const tone = healthyStates.has(normalized) ? 'positive'
    : pendingStates.has(normalized) ? 'warning'
      : normalized === 'FAILED' ? 'danger' : 'neutral'
  const label = normalized.toLowerCase().replaceAll('_', ' ')
  return <span className={`status-badge ${tone}`}><span className="status-dot" aria-hidden="true" />
    <span>{prefix ? `${prefix} ` : ''}{label}</span></span>
}
