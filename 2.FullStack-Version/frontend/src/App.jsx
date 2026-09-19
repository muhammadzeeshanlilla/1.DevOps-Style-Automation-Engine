import { useCallback, useEffect, useRef, useState } from 'react'
import './App.css'
import { getHealth, getStatus, getTasks } from './api/client'
import Header from './components/Header'
import Sidebar from './components/Sidebar'
import Dashboard from './pages/Dashboard'
import Logs from './pages/Logs'
import Settings from './pages/Settings'
import Tasks from './pages/Tasks'

const PAGE_TITLES = { dashboard: 'Dashboard', tasks: 'Tasks', logs: 'Activity Logs', settings: 'Settings' }

function App() {
  const [activePage, setActivePage] = useState('dashboard')
  const [health, setHealth] = useState(null)
  const [status, setStatus] = useState(null)
  const [tasks, setTasks] = useState(null)
  const [connection, setConnection] = useState('checking')
  const [initialLoading, setInitialLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [statusError, setStatusError] = useState('')
  const pollInFlight = useRef(false)

  const refreshStatus = useCallback(async ({ foreground = false } = {}) => {
    if (pollInFlight.current) return
    pollInFlight.current = true
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
      pollInFlight.current = false
      if (foreground) setRefreshing(false)
    }
  }, [])

  const refreshAll = useCallback(async ({ foreground = false } = {}) => {
    if (pollInFlight.current) return
    pollInFlight.current = true
    if (foreground) setRefreshing(true)
    try {
      const [healthResult, statusResult, tasksResult] = await Promise.allSettled([
        getHealth(), getStatus(), getTasks(),
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
      if (tasksResult.status === 'fulfilled') setTasks(tasksResult.value)
    } finally {
      pollInFlight.current = false
      if (foreground) setRefreshing(false)
      setInitialLoading(false)
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
    ? <Tasks data={tasks} onReload={() => refreshAll({ foreground: true })} />
    : activePage === 'logs'
      ? <Logs />
      : activePage === 'settings'
        ? <Settings tasks={tasks} />
        : <Dashboard health={health} status={status} tasks={tasks} connection={connection}
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
