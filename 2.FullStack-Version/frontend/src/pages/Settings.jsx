import { DatabaseIcon, GlobeIcon, InfoIcon, KeyIcon, LockKeyIcon } from '@phosphor-icons/react'
import { API_BASE_URL } from '../api/client'

export default function Settings({ tasks }) {
  const frontendUrl = window.location.origin
  return (
    <div className="page-stack">
      <div className="page-intro"><div><p className="section-kicker">Phase 1 environment</p><h2>Settings</h2>
        <p>Connection and privacy information for this local dashboard.</p></div></div>
      <div className="settings-grid">
        <section className="panel settings-panel"><div className="panel-heading"><div><p className="section-kicker">Local connection</p>
          <h2>Application URLs</h2></div></div>
          <dl className="settings-list">
            <div><dt><GlobeIcon />API URL</dt><dd><code>{API_BASE_URL}</code></dd></div>
            <div><dt><GlobeIcon />Frontend URL</dt><dd><code>{frontendUrl}</code></dd></div>
            <div><dt><DatabaseIcon />Configuration source</dt><dd>{tasks?.source === 'configuration_on_disk' ? 'Validated configuration on disk' : 'Backend configuration'}</dd></div>
          </dl>
        </section>
        <section className="panel settings-panel"><div className="panel-heading"><div><p className="section-kicker">Security boundary</p>
          <h2>Credentials stay on the backend</h2></div></div>
          <div className="privacy-copy"><span><LockKeyIcon size={29} weight="duotone" /></span>
            <p>SMTP credentials are supplied to the backend process through its environment. This frontend never requests, stores, or displays them.</p></div>
          <div className="alert info compact"><InfoIcon size={21} weight="fill" /><div><strong>Task editing is deferred</strong>
            <p>Creation, editing, deletion, authentication, and password fields are intentionally not part of Phase 1.</p></div></div>
        </section>
      </div>
      <section className="panel settings-panel"><div className="panel-heading"><div><p className="section-kicker">Safe operation</p>
        <h2>What this dashboard can access</h2></div></div>
        <div className="capability-list"><p><KeyIcon />Health and runtime status</p><p><KeyIcon />Cooperative engine start and stop</p>
          <p><KeyIcon />Read-only configured tasks</p><p><KeyIcon />Sanitized recent log summaries</p></div>
      </section>
    </div>
  )
}
