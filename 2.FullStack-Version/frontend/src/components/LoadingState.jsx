import { SpinnerGapIcon } from '@phosphor-icons/react'
export default function LoadingState({ label = 'Loading current data...' }) {
  return <div className="loading-state" role="status"><SpinnerGapIcon className="spin" size={24} /><span>{label}</span></div>
}
