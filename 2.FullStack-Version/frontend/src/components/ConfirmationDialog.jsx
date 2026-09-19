import { useEffect, useRef } from 'react'

export default function ConfirmationDialog({ jobId, busy, onCancel, onConfirm }) {
  const cancelRef = useRef(null)
  useEffect(() => {
    cancelRef.current?.focus()
    const closeOnEscape = (event) => {
      if (event.key === 'Escape' && !busy) onCancel()
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [busy, onCancel])

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) onCancel()
    }}>
      <section className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-title">
        <p className="section-kicker">Confirm deletion</p>
        <h2 id="delete-title">Delete monitoring job?</h2>
        <p>Delete monitoring job <code>{jobId}</code>? This removes its configuration and does not restart the engine.</p>
        <div className="button-row">
          <button ref={cancelRef} className="button outline" type="button" disabled={busy} onClick={onCancel}>Cancel</button>
          <button className="button danger" type="button" disabled={busy} onClick={onConfirm}>
            {busy ? 'Deleting...' : 'Delete job'}
          </button>
        </div>
      </section>
    </div>
  )
}
