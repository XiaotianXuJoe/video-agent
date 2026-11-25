import { useEffect, useState } from 'react'
import { CheckCircle2, Info, X, XCircle } from 'lucide-react'

export type ToastKind = 'info' | 'success' | 'error'
export interface ToastItem { id: number; kind: ToastKind; text: string }

type Listener = (t: ToastItem) => void
const listeners = new Set<Listener>()
let seq = 0

/** 轻量全局 toast：任意模块调用即可，无需 Context/Provider。几秒后自动消失。 */
export function toast(text: string, kind: ToastKind = 'info') {
  if (!text) return
  const item: ToastItem = { id: ++seq, kind, text }
  listeners.forEach(l => l(item))
}

export const toastError = (text: string) => toast(text, 'error')
export const toastSuccess = (text: string) => toast(text, 'success')

const ICONS = { info: Info, success: CheckCircle2, error: XCircle }
const TONE: Record<ToastKind, string> = {
  info: 'text-accent border-accent/30',
  success: 'text-greenx border-greenx/30',
  error: 'text-red-400 border-red-400/30',
}

export default function ToastContainer() {
  const [items, setItems] = useState<ToastItem[]>([])
  useEffect(() => {
    const listener: Listener = t => {
      setItems(prev => [...prev.slice(-3), t])
      setTimeout(() => setItems(prev => prev.filter(x => x.id !== t.id)), 4000)
    }
    listeners.add(listener)
    return () => { listeners.delete(listener) }
  }, [])

  if (items.length === 0) return null
  return (
    <div className="fixed bottom-4 right-4 z-[70] flex flex-col items-end gap-2 pointer-events-none">
      {items.map(t => {
        const Icon = ICONS[t.kind]
        return (
          <div key={t.id}
            className={`fade-in pointer-events-auto flex items-start gap-2 max-w-[min(90vw,420px)] bg-panel border rounded-xl px-3.5 py-2.5 text-sm ${TONE[t.kind]}`}
            style={{ boxShadow: 'var(--shadow)' }}>
            <Icon size={15} className="mt-0.5 shrink-0" />
            <span className="flex-1 break-words text-ink/90">{t.text}</span>
            <button onClick={() => setItems(prev => prev.filter(x => x.id !== t.id))}
              className="shrink-0 text-mute hover:text-ink" title="关闭"><X size={13} /></button>
          </div>
        )
      })}
    </div>
  )
}
