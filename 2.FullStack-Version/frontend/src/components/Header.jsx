import { CloudCheckIcon, CloudSlashIcon, SpinnerGapIcon } from '@phosphor-icons/react'
import StatusBadge from './StatusBadge'

export default function Header({ pageTitle, connection, engineState }) {
  const connected = connection === 'connected'
  const checking = connection === 'checking'
  const ConnectionIcon = checking ? SpinnerGapIcon : connected ? CloudCheckIcon : CloudSlashIcon
  return (
    <header className="top-header">
      <div><p className="eyebrow">{pageTitle}</p><h1>DevOps Automation Engine</h1></div>
      <div className="header-statuses" aria-label="Connection and engine status">
        <span className={`connection-indicator ${connection}`}>
          <ConnectionIcon className={checking ? 'spin' : ''} size={19} weight="bold" />
          {checking ? 'Checking API' : connected ? 'API Connected' : 'API Offline'}
        </span>
        <StatusBadge state={engineState || 'UNKNOWN'} prefix="Engine" />
      </div>
    </header>
  )
}
