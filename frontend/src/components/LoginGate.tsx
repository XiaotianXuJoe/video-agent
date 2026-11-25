import { useState } from 'react'
import { Clapperboard, Loader2, Lock } from 'lucide-react'
import { login } from '../lib/api'

/** 全站登录覆盖层：服务端 token 未配置 / 请求 401 时由 App 顶层渲染。 */
export default function LoginGate({ kind, onSuccess }: { kind: string; onSuccess: () => void }) {
  const [pwd, setPwd] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!pwd.trim() || busy) return
    setBusy(true); setErr('')
    try {
      await login(pwd.trim())
      onSuccess()
    } catch (ex: any) {
      const k = ex?.kind
      setErr(
        k === 'rate_limited' ? '尝试过于频繁，请一分钟后再试'
          : k === 'auth_unconfigured' ? '服务端尚未配置访问密码（auth.token），请先在服务器设置'
            : k === 'auth_failed' ? '密码错误'
              : (ex?.message || '登录失败'),
      )
      setBusy(false)
    }
  }

  return (
    <div className="h-full flex items-center justify-center bg-base px-4">
      <form onSubmit={submit} className="w-full max-w-sm bg-panel border border-edge rounded-2xl p-6"
        style={{ boxShadow: 'var(--shadow)' }}>
        <div className="flex items-center gap-2 mb-1">
          <Clapperboard size={20} className="text-accent" />
          <span className="font-semibold text-[16px]">讲解视频 Agent</span>
        </div>
        <p className="text-xs text-mute mb-5">请输入访问密码后继续。</p>

        {kind === 'auth_unconfigured' && (
          <div className="mb-4 text-xs text-amberx bg-amberx/10 border border-amberx/25 rounded-lg px-3 py-2 leading-5">
            服务端尚未配置访问密码：需要先在 <span className="mono">settings.json</span> 的
            <span className="mono"> auth.token </span>中设置，之后才能登录。
          </div>
        )}

        <label className="block mb-4">
          <span className="block text-xs text-mute mb-1">访问密码</span>
          <div className="relative">
            <Lock size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-mute" />
            <input autoFocus type="password" value={pwd} onChange={e => setPwd(e.target.value)}
              placeholder="访问密码" className="field-input mono pl-8" autoComplete="current-password" />
          </div>
        </label>

        {err && <div className="mb-3 text-xs text-red-400">{err}</div>}

        <button type="submit" disabled={busy || !pwd.trim()}
          className="w-full h-9 rounded-lg bg-accent text-onaccent text-sm font-medium hover:brightness-110 disabled:opacity-50 transition-all inline-flex items-center justify-center gap-1.5">
          {busy ? <Loader2 size={14} className="animate-spin" /> : null}
          {busy ? '登录中…' : '登录'}
        </button>
      </form>
    </div>
  )
}
