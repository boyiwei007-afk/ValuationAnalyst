import { useEffect, useRef, useId } from 'react'
import Icon from './Icons'
import { statusLabel } from './domain'

export function Status({ status, t }) { return <span className={`tag status-${status || 'created'}`}>{status === 'running' && <span className="mini-spinner"/>}{statusLabel(status, t)}</span> }
export function Modal({ title, children, onClose, className = '' }) {
  const ref = useRef(null)
  const heading = useId()
  useEffect(() => { const dialog = ref.current; dialog.showModal(); return () => dialog.close() }, [])
  return <dialog ref={ref} className={`modal ${className}`} aria-labelledby={heading} onCancel={e => { e.preventDefault(); onClose() }}><div className="modal-heading"><h2 id={heading}>{title}</h2><button className="icon-button" onClick={onClose} aria-label="关闭 / Close"><Icon name="close"/></button></div>{children}</dialog>
}
export function Empty({ icon = 'chart', title, children }) { return <div className="empty-state"><span><Icon name={icon} size={26}/></span><h3>{title}</h3><p>{children}</p></div> }
export function ErrorNotice({ message, onDismiss }) { return message ? <div className="error-notice" role="alert"><Icon name="alert" size={18}/><span>{message}</span>{onDismiss && <button onClick={onDismiss} aria-label="关闭 / Dismiss"><Icon name="close" size={16}/></button>}</div> : null }
export function Card({ title, subtitle, extra, children, className = '' }) { return <section className={`data-card ${className}`}><div className="card-heading"><div><h3>{title}</h3>{subtitle && <p>{subtitle}</p>}</div>{extra}</div>{children}</section> }
