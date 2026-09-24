import { useCallback, useEffect, useRef, useState } from 'react'
import './App.css'
import { getEmailSettings, getHealth, getMonitoringJobs, getStatus, getTasks } from './api/client'
import Header from './components/Header'
import Sidebar from './components/Sidebar'
import Dashboard from './pages/Dashboard'
import Logs from './pages/Logs'
import Settings from './pages/Settings'
import Tasks from './pages/Tasks'

const PAGE_TITLES = { dashboard: 'Dashboard', tasks: 'Monitoring Jobs', logs: 'Activity Logs', settings: 'Settings' }

function App() {
  const [activePage, setActivePage] = useState('dashboard')
  const [health, setHealth] = useState(null)
  const [status, setStatus] = useState(null)
  const [tasks, setTasks] = useState(null)
  const [monitoringJobs, setMonitoringJobs] = useState(null)
  const [monitoringJobsError, setMonitoringJobsError] = useState('')
  const [emailSettings, setEmailSettings] = useState(null)
  const [emailSettingsError, setEmailSettingsError] = useState('')
  const [connection, setConnection] = useState('checking')
  const [initialLoading, setInitialLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [statusError, setStatusError] = useState('')
  const statusPollInFlight = useRef(false)
  const configurationRequestId = useRef(0)

  const refreshStatus = useCallback(async ({ foreground = false } = {}) => {
    if (statusPollInFlight.current) return
    statusPollInFlight.current = true
    if (foreground) setRefreshing(true)
    try {
      setStatus(await getStatus())
      setConnection('connected')
      setStatusError('')
    } catch (error) {
      setConnection(error.status === 0 ? 'offline' : 'connected')
      setStatusError(error.status === 0
        ? 'The local API is unavailable. Start the backend, then try again.'
        : 'Engine status is temporarily unavailable. Retry in a moment.')
    } finally {
      statusPollInFlight.current = false
      if (foreground) setRefreshing(false)
    }
  }, [])

  const refreshAll = useCallback(async ({ foreground = false } = {}) => {
    const requestId = ++configurationRequestId.current
    if (foreground) setRefreshing(true)
    try {
      const [healthResult, statusResult, tasksResult, jobsResult, emailResult] = await Promise.allSettled([
        getHealth(), getStatus(), getTasks(), getMonitoringJobs(), getEmailSettings(),
      ])
      if (healthResult.status === 'fulfilled') {
        setHealth(healthResult.value)
        setConnection('connected')
      } else {
        setHealth(null)
        setConnection('offline')
      }
      if (statusResult.status === 'fulfilled') {
        setStatus(statusResult.value)
        setStatusError('')
      } else {
        setStatusError('The local API is unavailable. Start the backend, then try again.')
      }
      if (requestId === configurationRequestId.current) {
        if (tasksResult.status === 'fulfilled') setTasks(tasksResult.value)
        if (jobsResult.status === 'fulfilled') {
          setMonitoringJobs(jobsResult.value)
          setMonitoringJobsError('')
        } else {
          setMonitoringJobsError('Monitoring jobs could not be loaded.')
        }
        if (emailResult.status === 'fulfilled') {
          setEmailSettings(emailResult.value)
          setEmailSettingsError('')
        } else {
          setEmailSettingsError('Email settings could not be loaded.')
        }
      }
    } finally {
      if (foreground) setRefreshing(false)
      if (requestId === configurationRequestId.current) setInitialLoading(false)
    }
  }, [])

  useEffect(() => {
    const initialRequest = window.setTimeout(refreshAll, 0)
    return () => window.clearTimeout(initialRequest)
  }, [refreshAll])
  useEffect(() => {
    const interval = window.setInterval(() => refreshStatus(), 4000)
    return () => window.clearInterval(interval)
  }, [refreshStatus])

  const selectPage = (page) => {
    setActivePage(page)
    document.title = `${PAGE_TITLES[page]} | DevOps Automation Engine`
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  const page = activePage === 'tasks'
    ? <Tasks data={monitoringJobs} loadError={monitoringJobsError} email={emailSettings} engineState={status?.state}
        onReload={() => refreshAll({ foreground: true })} />
    : activePage === 'logs'
      ? <Logs />
      : activePage === 'settings'
        ? <Settings key={emailSettings?.sender || 'email-loading'} tasks={tasks} email={emailSettings}
            loadError={emailSettingsError} engineState={status?.state}
            onEmailUpdated={setEmailSettings} onReload={() => refreshAll({ foreground: true })} />
        : <Dashboard health={health} status={status} monitoringJobs={monitoringJobs} connection={connection}
            initialLoading={initialLoading} refreshing={refreshing} statusError={statusError}
            onRefresh={() => refreshAll({ foreground: true })} />

  return (
    <div className="app-shell">
      <Sidebar activePage={activePage} onNavigate={selectPage} />
      <div className="workspace">
        <Header pageTitle={PAGE_TITLES[activePage]} connection={connection} engineState={status?.state} />
        <main id="main-content" className="main-content">{page}</main>
      </div>
    </div>
  )
}

export default App
