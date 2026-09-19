import { ArrowClockwiseIcon, PlayIcon, SpinnerGapIcon, StopIcon } from '@phosphor-icons/react'
import { useState } from 'react'
import { startEngine, stopEngine } from '../api/client'

const ACTION_MESSAGES = {
  STARTING: 'Engine startup is in progress.',
  RUNNING: 'The engine is running.',
  STOPPING: 'The engine is stopping.',
  START_REQUESTED: 'Start requested. Waiting for runtime ownership to be confirmed.',
  STOPPED: 'The engine stopped and released runtime ownership.',
  REQUESTED: 'Cooperative stop requested. Waiting for workers to exit safely.',
}

export default function EngineControls({ engineState, refreshing, onRefresh }) {
  const [action, setAction] = useState('')
  const [feedback, setFeedback] = useState(null)
  const busy = Boolean(action) || refreshing
  const canStart = engineState === 'STOPPED'
  const canStop = engineState === 'RUNNING' || engineState === 'STARTING'

  const runAction = async (type) => {
    setAction(type)
    setFeedback(null)
    try {
      const result = type === 'start' ? await startEngine() : await stopEngine()
      setFeedback({ tone: 'success', text: ACTION_MESSAGES[result.state] || 'Request accepted by the engine.' })
      await onRefresh()
    } catch (error) {
      setFeedback({
        tone: error.status === 409 ? 'warning' : 'error',
        text: error.message || 'The request could not be completed.',
      })
    } finally {
      setAction('')
    }
  }

  return (
    <div className="engine-actions">
      <div className="button-row">
        <button className="button primary" type="button" disabled={!canStart || busy} onClick={() => runAction('start')}>
          {action === 'start' ? <SpinnerGapIcon className="spin" /> : <PlayIcon weight="fill" />}
          {action === 'start' ? 'Starting...' : 'Start Engine'}
        </button>
        <button className="button secondary" type="button" disabled={!canStop || busy} onClick={() => runAction('stop')}>
          {action === 'stop' ? <SpinnerGapIcon className="spin" /> : <StopIcon weight="fill" />}
          {action === 'stop' ? 'Stopping...' : 'Stop Engine'}
        </button>
        <button className="button outline" type="button" disabled={busy} onClick={onRefresh}>
          <ArrowClockwiseIcon className={refreshing ? 'spin' : ''} weight="bold" />
          {refreshing ? 'Refreshing...' : 'Refresh Status'}
        </button>
      </div>
      {feedback && <p className={`feedback ${feedback.tone}`} role="status">{feedback.text}</p>}
    </div>
  )
}
