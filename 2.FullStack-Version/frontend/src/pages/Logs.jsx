import { ArrowClockwiseIcon, FileTextIcon, WarningCircleIcon } from '@phosphor-icons/react'
import { useCallback, useEffect, useState } from 'react'
import { getLogs } from '../api/client'
import LoadingState from '../components/LoadingState'

export default function Logs() {
  const [limit, setLimit] = useState(20)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const loadLogs = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setData(await getLogs(limit))
    } catch (requestError) {
      setError(requestError.message)
    } finally {
      setLoading(false)
    }
  }, [limit])

  useEffect(() => {
    const initialRequest = window.setTimeout(loadLogs, 0)
    return () => window.clearTimeout(initialRequest)
  }, [loadLogs])

  return (
    <div className="page-stack">
      <div className="page-intro"><div><p className="section-kicker">Privacy-safe activity</p><h2>Activity Logs</h2>
        <p>Sanitized summaries from the backend, newest entries shown last.</p></div>
        <div className="log-actions"><label>Entries<select value={limit} onChange={(event) => setLimit(Number(event.target.value))}>
          <option value="20">20</option><option value="50">50</option><option value="100">100</option>
        </select></label>
        <button className="button outline" type="button" onClick={loadLogs} disabled={loading}>
          <ArrowClockwiseIcon className={loading ? 'spin' : ''} />Refresh</button></div>
      </div>
      {error && <div className="alert error" role="alert"><WarningCircleIcon size={22} weight="fill" />
        <div><strong>Logs are unavailable</strong><p>{error}</p></div></div>}
      <section className="panel log-panel">
        <div className="panel-heading"><div><p className="section-kicker">Sanitized engine log</p>
          <h2>{data ? `${data.count} recent entr${data.count === 1 ? 'y' : 'ies'}` : 'Recent entries'}</h2></div>
          {data?.truncated && <span className="subtle-label">Earlier entries omitted</span>}</div>
        {loading && !data ? <LoadingState label="Loading activity logs..." /> : data?.entries?.length ? (
          <ol className="log-list">{data.entries.map((entry, index) => (
            <li key={`${entry.timestamp}-${index}`}><time dateTime={entry.timestamp}>{entry.timestamp}</time>
              <span className={`log-level ${entry.level.toLowerCase()}`}>{entry.level}</span><p>{entry.message}</p></li>
          ))}</ol>
        ) : <div className="empty-state"><FileTextIcon size={38} /><strong>No log entries available</strong>
          <p>The backend did not return any sanitized entries.</p></div>}
      </section>
    </div>
  )
}
