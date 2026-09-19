import {
  ActivityIcon, CheckCircleIcon, ClockCountdownIcon, CpuIcon, FileTextIcon,
  FolderOpenIcon, ListChecksIcon, PlugsConnectedIcon, QueueIcon, WarningCircleIcon,
} from '@phosphor-icons/react'
import EngineControls from '../components/EngineControls'
import LoadingState from '../components/LoadingState'
import StatusBadge from '../components/StatusBadge'
import StatusCard from '../components/StatusCard'

function displayWorker(state, stopped) {
  if (stopped) return { label: 'Inactive', state: 'stopped' }
  if (state === 'alive') return { label: 'Active', state: 'alive' }
  if (state === 'stopped') return { label: 'Inactive', state: 'stopped' }
  return { label: 'Unavailable', state: 'unavailable' }
}

function displayMonitors(monitors, stopped, available) {
  if (stopped) return { label: 'Inactive', state: 'stopped', note: 'Engine is stopped' }
  if (!available) return { label: 'Unavailable', state: 'unavailable', note: 'Runtime details unavailable' }
  if (!monitors.length) return { label: 'Not configured', state: 'unavailable', note: 'No file monitors reported' }
  if (monitors.every((item) => item.state === 'alive')) {
    return { label: 'Active', state: 'alive', note: `${monitors.length} monitor${monitors.length === 1 ? '' : 's'} reporting` }
  }
  if (monitors.some((item) => item.state === 'alive')) return { label: 'Degraded', state: 'unavailable', note: 'Some monitors unavailable' }
  return { label: 'Inactive', state: 'stopped', note: 'No active monitors' }
}

export default function Dashboard({
  health, status, tasks, connection, initialLoading, refreshing, statusError, onRefresh,
}) {
  if (initialLoading && !status) return <LoadingState label="Connecting to the local automation API..." />

  const engineState = status?.state || 'UNKNOWN'
  const stopped = engineState === 'STOPPED'
  const runtime = status?.runtime
  const snapshot = runtime?.available ? runtime.snapshot : null
  const scheduler = displayWorker(snapshot?.scheduler, stopped)
  const monitors = displayMonitors(snapshot?.monitors || [], stopped, Boolean(snapshot))
  const configuredCount = snapshot?.configured_count ?? tasks?.count
  const enabledCount = snapshot?.enabled_count ?? tasks?.tasks?.filter((task) => task.enabled).length
  const current = snapshot?.current
  const lastResult = snapshot?.last_result

  return (
    <div className="page-stack">
      {statusError && (
        <div className="alert error" role="alert">
          <WarningCircleIcon size={22} weight="fill" />
          <div><strong>Unable to refresh engine status</strong><p>{statusError}</p></div>
          <button type="button" onClick={onRefresh}>Retry</button>
        </div>
      )}

      <div className="dashboard-grid">
        <section className="panel engine-panel" aria-labelledby="engine-status-title">
          <div className="panel-heading"><div><p className="section-kicker">Runtime control</p><h2 id="engine-status-title">Engine Status</h2></div>
            <StatusBadge state={engineState} /></div>
          <div className="engine-summary">
            <span className={`engine-emblem ${engineState.toLowerCase()}`}><CpuIcon size={43} weight="duotone" /></span>
            <div>
              <p className="engine-label">Current state</p>
              <strong className="engine-state">{engineState.toLowerCase()}</strong>
              <p>{stopped
                ? 'The automation engine is not running. Start it to activate scheduled tasks and file monitoring.'
                : engineState === 'RUNNING'
                  ? 'The automation engine owns the runtime and is processing configured workflows.'
                  : 'The engine is changing state. Status will refresh automatically.'}</p>
            </div>
          </div>
          <EngineControls engineState={engineState} refreshing={refreshing} onRefresh={onRefresh} />
          {!snapshot && (
            <p className="runtime-note">
              <ActivityIcon size={19} />
              {stopped ? 'Runtime details are unavailable while the engine is stopped.' : runtime?.reason || 'Live runtime details are currently unavailable.'}
            </p>
          )}
        </section>

        <section className="panel health-panel" aria-labelledby="system-health-title">
          <div className="panel-heading"><div><p className="section-kicker">Live services</p><h2 id="system-health-title">System Health</h2></div></div>
          <div className="health-list">
            <div className="health-row">
              <span className="health-icon"><PlugsConnectedIcon size={23} weight="duotone" /></span>
              <div><strong>API</strong><small>{health ? 'Local API is reachable' : 'No health response'}</small></div>
              <StatusBadge state={connection === 'connected' ? 'connected' : 'Unavailable'} />
            </div>
            <div className="health-row">
              <span className="health-icon"><CpuIcon size={23} weight="duotone" /></span>
              <div><strong>Engine Runtime</strong><small>{status?.pid ? `Process ${status.pid}` : 'No owned process'}</small></div>
              <StatusBadge state={engineState} />
            </div>
            <div className="health-row">
              <span className="health-icon"><ClockCountdownIcon size={23} weight="duotone" /></span>
              <div><strong>Scheduler</strong><small>{stopped ? 'Engine is stopped' : 'Runtime scheduler worker'}</small></div>
              <StatusBadge state={scheduler.state} />
            </div>
            <div className="health-row">
              <span className="health-icon"><FolderOpenIcon size={23} weight="duotone" /></span>
              <div><strong>File Monitor</strong><small>{monitors.note}</small></div>
              <StatusBadge state={monitors.state} />
            </div>
          </div>
        </section>
      </div>

      <section className="panel overview-panel" aria-labelledby="task-overview-title">
        <div className="panel-heading"><div><p className="section-kicker">Current workload</p><h2 id="task-overview-title">Task Overview</h2></div></div>
        <div className="metrics-grid">
          <StatusCard icon={ListChecksIcon} label="Configured Tasks" value={configuredCount ?? 'Unavailable'} note={tasks ? 'From validated configuration' : 'Configuration unavailable'} />
          <StatusCard icon={CheckCircleIcon} label="Enabled Tasks" value={enabledCount ?? 'Unavailable'} note="Ready when the engine runs" tone="positive" />
          <StatusCard icon={QueueIcon} label="Queued Events" value={snapshot ? snapshot.queued_events : 'Unavailable'} note={snapshot ? 'Waiting for processing' : 'Requires live runtime'} tone="warning" />
          <StatusCard icon={ActivityIcon} label="Current Task" value={current?.id || 'None'} note={current ? `${current.type} / ${current.phase}` : 'No current task reported'} />
        </div>
      </section>

      <section className="panel execution-panel" aria-labelledby="recent-execution-title">
        <div className="panel-heading"><div><p className="section-kicker">Latest runtime detail</p><h2 id="recent-execution-title">Recent Execution</h2></div></div>
        <div className="execution-grid">
          <div className="execution-item"><span><FileTextIcon size={24} weight="duotone" /></span><div>
            <p>Last Task Result</p><strong>{lastResult?.status || 'None'}</strong>
            <small>{lastResult ? `Task ${lastResult.id}` : 'No runtime result is available'}</small>
          </div></div>
          <div className="execution-item"><span><ActivityIcon size={24} weight="duotone" /></span><div>
            <p>Current Running Task</p><strong>{current?.id || 'None'}</strong>
            <small>{current ? `Started ${new Date(current.started_at).toLocaleString()}` : 'No task is currently running'}</small>
          </div></div>
        </div>
      </section>
    </div>
  )
}
