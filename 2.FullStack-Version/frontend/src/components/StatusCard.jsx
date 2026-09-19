export default function StatusCard({ icon: Icon, label, value, note, tone = 'neutral' }) {
  return (
    <div className="metric">
      <span className={`metric-icon ${tone}`} aria-hidden="true"><Icon size={23} weight="duotone" /></span>
      <div><p>{label}</p><strong>{value}</strong>{note && <small>{note}</small>}</div>
    </div>
  )
}
