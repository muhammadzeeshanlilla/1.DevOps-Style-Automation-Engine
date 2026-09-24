import {
  CheckCircleIcon, DatabaseIcon, EyeIcon, EyeSlashIcon, GlobeIcon, InfoIcon,
  KeyIcon, LockKeyIcon, PaperPlaneTiltIcon, TrashIcon, WarningCircleIcon,
} from '@phosphor-icons/react'
import { useState } from 'react'
import {
  API_BASE_URL, forgetEmailCredential, sendTestEmail, updateEmailSettings,
} from '../api/client'

export default function Settings({ tasks, email, loadError, engineState, onEmailUpdated, onReload }) {
  const [form, setForm] = useState(() => ({
    sender: email?.sender || '',
    receiver: email?.receiver || '',
    smtp_server: email?.smtp_server || 'smtp.gmail.com',
    smtp_port: email?.smtp_port || 587,
    app_password: '',
    remember_credential: true,
  }))
  const [showPassword, setShowPassword] = useState(false)
  const [busy, setBusy] = useState('')
  const [feedback, setFeedback] = useState(null)
  const stopped = engineState === 'STOPPED'
  const frontendUrl = window.location.origin

  const set = (field, value) => setForm((current) => ({ ...current, [field]: value }))

  if (loadError) return <div className="load-failure" role="alert">
    <WarningCircleIcon size={34} weight="fill" />
    <strong>Email settings could not be loaded.</strong>
    <p>The backend configuration is unavailable or invalid. Correct it, then retry.</p>
    <button className="button outline" type="button" onClick={onReload}>Retry</button>
  </div>

  const save = async (event) => {
    event.preventDefault()
    setBusy('save')
    setFeedback(null)
    try {
      const updated = await updateEmailSettings({
        ...form, smtp_port: Number(form.smtp_port),
        app_password: form.app_password || null,
      })
      set('app_password', '')
      setShowPassword(false)
      onEmailUpdated(updated)
      setFeedback({ type: 'success', text: 'Email settings saved. The password field has been cleared.' })
    } catch (error) {
      setFeedback({ type: 'error', text: error.message })
    } finally { setBusy('') }
  }

  const test = async () => {
    setBusy('test')
    setFeedback(null)
    try {
      const result = await sendTestEmail()
      setFeedback({ type: 'success', text: result.message })
    } catch (error) {
      setFeedback({ type: 'error', text: error.message })
    } finally { setBusy('') }
  }

  const forget = async () => {
    if (!window.confirm('Forget the saved App Password from this device?')) return
    setBusy('forget')
    setFeedback(null)
    try {
      const result = await forgetEmailCredential()
      set('app_password', '')
      onEmailUpdated({ ...email, credential_saved: false, credential_persisted: false })
      setFeedback({ type: 'success', text: result.message })
    } catch (error) {
      setFeedback({ type: 'error', text: error.message })
    } finally { setBusy('') }
  }

  return (
    <div className="page-stack">
      <div className="page-intro"><div><p className="section-kicker">Local secure configuration</p><h2>Settings</h2>
        <p>Configure email delivery without writing credentials to project files.</p></div></div>

      {!stopped && <div className="alert info"><InfoIcon size={22} weight="fill" />
        <div><strong>Email settings are locked</strong><p>Stop the engine before changing email settings or sending a test email.</p></div></div>}

      <section className="panel email-delivery-panel">
        <div className="panel-heading"><div><p className="section-kicker">Global delivery configuration</p>
          <h2>Email Delivery</h2></div>
          <span className={email?.credential_saved ? 'credential-badge saved' : 'credential-badge'}>
            {email?.credential_saved ? <CheckCircleIcon weight="fill" /> : <WarningCircleIcon weight="fill" />}
            {email?.credential_saved
              ? email.credential_persisted ? 'App Password saved securely' : 'Session credential ready'
              : 'No App Password saved'}
          </span>
        </div>

        <div className="password-warning"><LockKeyIcon size={27} weight="duotone" /><div>
          <strong>Do NOT enter your normal Gmail password. Use a Google App Password.</strong>
          <p>Your App Password is stored securely on this device and is never written to project files, JSON configuration, logs, Git, localStorage, or sessionStorage.</p>
        </div></div>

        <form className="email-form" onSubmit={save}>
          <div className="form-grid">
            <label>Sender Email<input type="email" required autoComplete="username" value={form.sender}
              disabled={!stopped || Boolean(busy)} onChange={(event) => set('sender', event.target.value)} /></label>
            <label>Receiver Email<input type="email" required value={form.receiver}
              disabled={!stopped || Boolean(busy)} onChange={(event) => set('receiver', event.target.value)} /></label>
          </div>
          <label>Gmail App Password
            <span className="password-input">
              <input type={showPassword ? 'text' : 'password'} autoComplete="new-password"
                value={form.app_password} disabled={!stopped || Boolean(busy)}
                placeholder={email?.credential_saved ? 'Leave blank to keep current credential' : 'Enter Google App Password'}
                onChange={(event) => set('app_password', event.target.value)} />
              <button type="button" disabled={!form.app_password || Boolean(busy)}
                aria-label={showPassword ? 'Hide App Password' : 'Show App Password'}
                onClick={() => setShowPassword((current) => !current)}>
                {showPassword ? <EyeSlashIcon /> : <EyeIcon />}
              </button>
            </span>
            <small>The password is write-only. Google commonly displays App Passwords as 16 characters.</small>
          </label>
          <label className="check-field"><input type="checkbox" checked={form.remember_credential}
            disabled={!stopped || Boolean(busy)}
            onChange={(event) => set('remember_credential', event.target.checked)} />
            Remember App Password securely on this device
          </label>
          <details className="advanced-smtp">
            <summary>Advanced SMTP Settings</summary>
            <p>Gmail defaults are used unless you provide settings for another SMTP provider.</p>
            <div className="form-grid">
              <label>SMTP Server<input required value={form.smtp_server}
                disabled={!stopped || Boolean(busy)} onChange={(event) => set('smtp_server', event.target.value)} /></label>
              <label>SMTP Port<input type="number" min="1" max="65535" required value={form.smtp_port}
                disabled={!stopped || Boolean(busy)} onChange={(event) => set('smtp_port', event.target.value)} /></label>
            </div>
          </details>
          {!email?.secure_storage_available && <div className="alert error compact" role="alert">
            <WarningCircleIcon weight="fill" /><div><strong>Secure device storage unavailable</strong>
              <p>You can use a session-only credential, but it is cleared when the backend stops.</p></div></div>}
          {feedback && <p className={'feedback ' + feedback.type} role={feedback.type === 'error' ? 'alert' : 'status'}>{feedback.text}</p>}
          <div className="button-row email-actions">
            <button className="button primary" type="submit" disabled={!stopped || Boolean(busy)}>
              <KeyIcon />{busy === 'save' ? 'Saving...' : 'Save Email Settings'}</button>
            <button className="button secondary" type="button" disabled={!stopped || Boolean(busy) || !email?.credential_saved} onClick={test}>
              <PaperPlaneTiltIcon />{busy === 'test' ? 'Sending...' : 'Send Test Email'}</button>
            <button className="button text-danger" type="button" disabled={!stopped || Boolean(busy) || !email?.credential_saved} onClick={forget}>
              <TrashIcon />{busy === 'forget' ? 'Forgetting...' : 'Forget Saved App Password'}</button>
          </div>
          <p className="acceptance-note">SMTP acceptance does not guarantee inbox delivery. Check the receiver inbox and spam folder after a successful test.</p>
        </form>
      </section>

      <section className="panel help-panel">
        <div className="panel-heading"><div><p className="section-kicker">Google Account help</p>
          <h2>How to create a Google App Password</h2></div></div>
        <ol>
          <li>Sign in to your Google Account.</li><li>Open Google Account Security.</li>
          <li>Enable 2-Step Verification if it is not already enabled.</li><li>Open App passwords.</li>
          <li>Create a new App Password for this application.</li><li>Copy the generated 16-character App Password here.</li>
          <li>Do not share the App Password or commit it to Git.</li>
        </ol>
        <a className="button outline help-link" href="https://myaccount.google.com/apppasswords"
          target="_blank" rel="noreferrer">Open Google App passwords</a>
        <p className="help-note">If App passwords is not available for your Google account, your account may not currently be eligible for this authentication method.</p>
      </section>

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
          <h2>How the engine receives credentials</h2></div></div>
          <div className="privacy-copy"><span><LockKeyIcon size={29} weight="duotone" /></span>
            <p>The API retrieves the credential at engine start and passes it only through the child process environment as SMTP_PASSWORD. Direct CLI environment usage still works.</p></div>
        </section>
      </div>
    </div>
  )
}
