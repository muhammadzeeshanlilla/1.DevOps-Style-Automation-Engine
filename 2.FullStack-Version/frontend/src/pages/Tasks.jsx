import { ArrowClockwiseIcon, ClockIcon, InfoIcon, ListChecksIcon } from '@phosphor-icons/react'
import LoadingState from '../components/LoadingState'

function triggerText(trigger) {
  if (trigger.type === 'daily') return `Daily at ${String(trigger.hour).padStart(2, '0')}:${String(trigger.minute).padStart(2, '0')}`
  if (trigger.type === 'interval') return `Every ${trigger.every_minutes} minute${trigger.every_minutes === 1 ? '' : 's'}`
  return trigger.events?.length ? trigger.events.join(', ') : 'File event'
}

export default function Tasks({ data, onReload }) {
  if (!data) return <LoadingState label="Loading configured tasks..." />
  return (
    <div className="page-stack">
      <div className="page-intro"><div><p className="section-kicker">Read-only configuration</p><h2>Configured Tasks</h2>
        <p>Only privacy-safe fields returned by the backend are shown.</p></div>
        <button className="button outline" type="button" onClick={onReload}><ArrowClockwiseIcon />Refresh</button>
      </div>
      {data.changes_require_restart && <div className="alert info"><InfoIcon size={22} weight="fill" />
        <div><strong>Configuration changes require engine restart</strong><p>This Phase 1 view does not edit the configuration.</p></div></div>}
      <section className="panel">
        <div className="panel-heading"><div><p className="section-kicker">Configuration on disk</p>
          <h2>{data.count} task{data.count === 1 ? '' : 's'}</h2></div></div>
        {data.tasks.length === 0 ? <div className="empty-state"><ListChecksIcon size={38} /><strong>No tasks configured</strong>
          <p>The validated configuration currently contains no tasks.</p></div> : (
          <div className="task-table-wrap"><table className="data-table"><thead><tr>
            <th>Task ID</th><th>Type</th><th>Status</th><th>Trigger</th>
          </tr></thead><tbody>{data.tasks.map((task) => <tr key={task.id}>
            <td data-label="Task ID"><code>{task.id}</code></td>
            <td data-label="Type">{task.type.replaceAll('_', ' ')}</td>
            <td data-label="Status"><span className={task.enabled ? 'inline-status enabled' : 'inline-status disabled'}>
              {task.enabled ? 'Enabled' : 'Disabled'}</span></td>
            <td data-label="Trigger"><span className="trigger"><ClockIcon />{triggerText(task.trigger)}</span></td>
          </tr>)}</tbody></table></div>
        )}
      </section>
    </div>
  )
}
