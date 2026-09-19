import { CheckCircleIcon, FolderOpenIcon, XCircleIcon } from '@phosphor-icons/react'
import { useState } from 'react'
import { createMonitoringJob, updateMonitoringJob, validateFolder } from '../api/client'

const emptyForm = {
  id: '', folder_path: '', scheduleType: 'daily',
  hour: 9, minute: 0, interval: 30, enabled: true,
}

function fromJob(job) {
  if (!job) return emptyForm
  const daily = job.schedule.type === 'daily'
  return {
    id: job.id, folder_path: job.folder_path,
    scheduleType: daily ? 'daily' : 'minutes',
    hour: job.schedule.hour ?? 9, minute: job.schedule.minute ?? 0,
    interval: job.schedule.every_minutes ?? 30, enabled: job.enabled,
  }
}

export default function MonitoringJobForm({ job, email, onSaved, onCancel }) {
  const [form, setForm] = useState(() => fromJob(job))
  const [folderState, setFolderState] = useState('idle')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const set = (field, value) => {
    setForm((current) => ({ ...current, [field]: value }))
    if (field === 'folder_path') setFolderState('idle')
  }

  const checkFolder = async () => {
    setBusy(true)
    setMessage('')
    try {
      const result = await validateFolder(form.folder_path)
      setFolderState(result.valid ? 'valid' : 'invalid')
      if (!result.valid) setMessage('Folder could not be used. Confirm that it exists, is a directory, and is accessible to the backend.')
    } catch (error) {
      setFolderState('invalid')
      setMessage(error.message)
    } finally { setBusy(false) }
  }

  const save = async (event) => {
    event.preventDefault()
    if (folderState !== 'valid') {
      setMessage('Validate the folder before saving this monitoring job.')
      return
    }
    if (!job && !/^[a-z0-9_-]+$/.test(form.id)) {
      setMessage('Job ID may use lowercase letters, numbers, hyphens, and underscores only.')
      return
    }
    const schedule = form.scheduleType === 'daily'
      ? { type: 'daily', hour: Number(form.hour), minute: Number(form.minute) }
      : { type: form.scheduleType, every: Number(form.interval) }
    const body = { folder_path: form.folder_path, schedule, enabled: form.enabled }
    setBusy(true)
    setMessage('')
    try {
      if (job) await updateMonitoringJob(job.id, body)
      else await createMonitoringJob({ id: form.id, ...body })
      onSaved(job ? 'Monitoring job updated.' : 'Monitoring job created.')
    } catch (error) {
      setMessage(error.message)
    } finally { setBusy(false) }
  }

  return (
    <section className="panel job-form-panel">
      <div className="panel-heading"><div><p className="section-kicker">{job ? 'Update configuration' : 'New workflow'}</p>
        <h2>{job ? 'Edit ' + job.id : 'Create Monitoring Job'}</h2></div></div>
      <form className="job-form" onSubmit={save}>
        <label>Job ID
          <input value={form.id} disabled={Boolean(job) || busy} required pattern="[a-z0-9_-]+"
            placeholder="daily-reports" onChange={(event) => set('id', event.target.value)} />
          <small>Lowercase letters, numbers, hyphens, and underscores.</small>
        </label>
        <div className="folder-field"><label>Folder to Monitor
          <input value={form.folder_path} disabled={busy} required placeholder="C:\Projects\Reports"
            onChange={(event) => set('folder_path', event.target.value)} />
        </label>
          <button className="button outline" type="button" disabled={busy || !form.folder_path.trim()} onClick={checkFolder}>
            <FolderOpenIcon />Validate Folder
          </button>
        </div>
        {folderState !== 'idle' && <p className={'validation-result ' + folderState} role="status">
          {folderState === 'valid' ? <CheckCircleIcon weight="fill" /> : <XCircleIcon weight="fill" />}
          {folderState === 'valid' ? 'Folder is valid and accessible.' : 'Folder could not be used.'}
        </p>}
        <fieldset><legend>Report Schedule</legend>
          <div className="schedule-options">
            <label><input type="radio" name="schedule" checked={form.scheduleType === 'daily'} onChange={() => set('scheduleType', 'daily')} />Daily</label>
            <label><input type="radio" name="schedule" checked={form.scheduleType === 'minutes'} onChange={() => set('scheduleType', 'minutes')} />Every X minutes</label>
            <label><input type="radio" name="schedule" checked={form.scheduleType === 'hours'} onChange={() => set('scheduleType', 'hours')} />Every X hours</label>
          </div>
          {form.scheduleType === 'daily' ? <div className="time-fields">
            <label>Hour<input type="number" min="0" max="23" required value={form.hour} onChange={(event) => set('hour', event.target.value)} /></label>
            <span>:</span><label>Minute<input type="number" min="0" max="59" required value={form.minute} onChange={(event) => set('minute', event.target.value)} /></label>
          </div> : <label className="interval-field">Interval
            <input type="number" min="1" required value={form.interval} onChange={(event) => set('interval', event.target.value)} />
            <span>{form.scheduleType}</span>
          </label>}
        </fieldset>
        <label className="check-field"><input type="checkbox" checked={form.enabled} onChange={(event) => set('enabled', event.target.checked)} />Enabled</label>
        <div className="readonly-detail"><strong>Report recipient</strong><span>{email?.receiver || 'Unavailable'}</span>
          <small>Shared backend email setting; it is not stored per job.</small></div>
        <div className="readonly-events"><strong>All supported changes are monitored</strong>
          <span><CheckCircleIcon />New files</span><span><CheckCircleIcon />Modified files</span><span><CheckCircleIcon />Deleted files</span>
        </div>
        {message && <p className="feedback error" role="alert">{message}</p>}
        <div className="button-row">
          <button className="button primary" type="submit" disabled={busy}>{busy ? 'Working...' : 'Save Monitoring Job'}</button>
          <button className="button outline" type="button" disabled={busy} onClick={onCancel}>Cancel</button>
        </div>
      </form>
    </section>
  )
}
