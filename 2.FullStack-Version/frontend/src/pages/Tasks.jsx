import {
  ArrowClockwiseIcon, ClockIcon, FolderOpenIcon, InfoIcon, ListChecksIcon,
  PencilSimpleIcon, PlusIcon, TrashIcon,
} from '@phosphor-icons/react'
import { useState } from 'react'
import { deleteMonitoringJob, setMonitoringJobEnabled } from '../api/client'
import ConfirmationDialog from '../components/ConfirmationDialog'
import LoadingState from '../components/LoadingState'
import MonitoringJobForm from '../components/MonitoringJobForm'

function scheduleText(schedule) {
  if (schedule.type === 'daily') {
    return 'Daily at ' + String(schedule.hour).padStart(2, '0') + ':' + String(schedule.minute).padStart(2, '0')
  }
  return 'Every ' + schedule.every_minutes + ' minute' + (schedule.every_minutes === 1 ? '' : 's')
}

function safePath(path) {
  if (path.length <= 52) return path
  const parts = path.split(/[\\/]/).filter(Boolean)
  return '...\\' + parts.slice(-2).join('\\')
}

export default function Tasks({ data, loadError, email, engineState, onReload }) {
  const [editing, setEditing] = useState(null)
  const [creating, setCreating] = useState(false)
  const [deleting, setDeleting] = useState(null)
  const [busyId, setBusyId] = useState('')
  const [feedback, setFeedback] = useState(null)
  const stopped = engineState === 'STOPPED'

  const finish = async (message) => {
    setCreating(false)
    setEditing(null)
    setFeedback({ message, type: 'success' })
    await onReload()
  }

  const toggle = async (job) => {
    setBusyId(job.id)
    setFeedback(null)
    try {
      await setMonitoringJobEnabled(job.id, !job.enabled)
      await finish(job.enabled ? 'Monitoring job disabled.' : 'Monitoring job enabled.')
    } catch (error) { setFeedback({ message: error.message, type: 'error' }) } finally { setBusyId('') }
  }

  const remove = async () => {
    setBusyId(deleting.id)
    setFeedback(null)
    try {
      await deleteMonitoringJob(deleting.id)
      setDeleting(null)
      await finish('Monitoring job deleted.')
    } catch (error) { setFeedback({ message: error.message, type: 'error' }) } finally { setBusyId('') }
  }

  if (loadError) return <div className="load-failure" role="alert">
    <InfoIcon size={34} weight="fill" />
    <strong>Monitoring jobs could not be loaded.</strong>
    <p>The backend configuration is unavailable or invalid. Correct it, then retry.</p>
    <button className="button outline" type="button" onClick={onReload}><ArrowClockwiseIcon />Retry</button>
  </div>
  if (!data) return <LoadingState label="Loading monitoring jobs..." />
  if (creating || editing) return <MonitoringJobForm job={editing} email={email}
    onSaved={finish} onCancel={() => { setCreating(false); setEditing(null) }} />

  return (
    <div className="page-stack">
      <div className="page-intro"><div><p className="section-kicker">Folder report workflows</p><h2>Monitoring Jobs</h2>
        <p>Monitor local folders and email accumulated file activity on a real engine schedule.</p></div>
        <div className="button-row">
          <button className="button outline" type="button" onClick={onReload}><ArrowClockwiseIcon />Refresh</button>
          <button className="button primary" type="button" disabled={!stopped} onClick={() => setCreating(true)}>
            <PlusIcon />Create Monitoring Job
          </button>
        </div>
      </div>
      {!stopped && <div className="alert info"><InfoIcon size={22} weight="fill" />
        <div><strong>Configuration is locked</strong><p>Stop the engine before changing monitoring jobs.</p></div></div>}
      {feedback && <p className={'feedback ' + feedback.type} role={feedback.type === 'error' ? 'alert' : 'status'}>{feedback.message}</p>}
      <section className="panel">
        <div className="panel-heading"><div><p className="section-kicker">Configuration on disk</p>
          <h2>{data.count} monitoring job{data.count === 1 ? '' : 's'}</h2></div></div>
        {data.jobs.length === 0 ? <div className="empty-state"><ListChecksIcon size={38} /><strong>No monitoring jobs configured</strong>
          <p>Create a job while the engine is stopped.</p></div> : (
          <div className="job-list">{data.jobs.map((job) => <article className="job-card" key={job.id}>
            <div className="job-card-main"><span className="job-folder-icon"><FolderOpenIcon weight="duotone" /></span><div>
              <div className="job-title"><code>{job.id}</code>
                <span className={job.enabled ? 'inline-status enabled' : 'inline-status disabled'}>{job.enabled ? 'Enabled' : 'Disabled'}</span></div>
              <p className="safe-path">{safePath(job.folder_path)}</p>
              <p className="trigger"><ClockIcon />{scheduleText(job.schedule)}</p>
            </div></div>
            <div className="job-actions">
              <label className="toggle"><input type="checkbox" checked={job.enabled}
                disabled={!stopped || busyId === job.id} onChange={() => toggle(job)} /><span aria-hidden="true" />
                <b>{job.enabled ? 'Enabled' : 'Disabled'}</b></label>
              <button className="button outline" type="button" disabled={!stopped} onClick={() => setEditing(job)}>
                <PencilSimpleIcon />Edit</button>
              <button className="button text-danger" type="button" disabled={!stopped} onClick={() => setDeleting(job)}>
                <TrashIcon />Delete</button>
            </div>
          </article>)}</div>
        )}
      </section>
      <div className="alert info"><InfoIcon size={22} weight="fill" /><div><strong>File activity included</strong>
        <p>Folder reports collect new, modified, and deleted files. Event filtering is not supported in this phase.</p></div></div>
      {deleting && <ConfirmationDialog jobId={deleting.id} busy={busyId === deleting.id}
        onCancel={() => setDeleting(null)} onConfirm={remove} />}
    </div>
  )
}
