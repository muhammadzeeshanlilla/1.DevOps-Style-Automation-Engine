import { ActivityIcon, GearSixIcon, HouseIcon, ListChecksIcon, TerminalWindowIcon } from '@phosphor-icons/react'

const navigation = [
  { id: 'dashboard', label: 'Dashboard', icon: HouseIcon },
  { id: 'tasks', label: 'Tasks', icon: ListChecksIcon },
  { id: 'logs', label: 'Activity Logs', icon: ActivityIcon },
  { id: 'settings', label: 'Settings', icon: GearSixIcon },
]

export default function Sidebar({ activePage, onNavigate }) {
  return (
    <aside className="sidebar" aria-label="Primary navigation">
      <div className="brand">
        <span className="brand-icon" aria-hidden="true"><TerminalWindowIcon size={25} weight="duotone" /></span>
        <span><strong>DevOps Automation Engine</strong><small>Local control plane</small></span>
      </div>
      <nav className="sidebar-nav">
        {navigation.map(({ id, label, icon: Icon }) => (
          <button className={activePage === id ? 'nav-item active' : 'nav-item'} type="button"
            key={id} onClick={() => onNavigate(id)} aria-current={activePage === id ? 'page' : undefined}>
            <Icon size={21} weight={activePage === id ? 'fill' : 'regular'} /><span>{label}</span>
          </button>
        ))}
      </nav>
      <p className="sidebar-note">Automation for smarter operations.</p>
    </aside>
  )
}
