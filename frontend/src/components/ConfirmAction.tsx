import { useState } from 'react'

// Browser-native confirm dialogs can be suppressed in embedded app browsers.
export function ConfirmAction({ label, message, onConfirm, disabled, danger = false }: {
  label: string; message: string; onConfirm: () => void; disabled?: boolean; danger?: boolean
}) {
  const [confirming, setConfirming] = useState(false)
  return confirming ? <div className="rounded-lg border border-slate-200 p-3" role="group" aria-label={label}>
    <p className="mb-2 text-sm">{message}</p>
    <div className="flex gap-2"><button className={danger ? 'btn-danger' : 'btn-secondary'} disabled={disabled} onClick={() => { setConfirming(false); onConfirm() }}>Confirm {label.toLowerCase()}</button><button className="btn-secondary" disabled={disabled} onClick={() => setConfirming(false)}>Cancel</button></div>
  </div> : <button className={danger ? 'btn-danger' : 'btn-secondary'} disabled={disabled} onClick={() => setConfirming(true)}>{label}</button>
}
