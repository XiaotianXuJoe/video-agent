import { useEffect, useRef, useState } from 'react'
import { Plus, Trash2, X, Eye, EyeOff, Loader2, CheckCircle2, XCircle } from 'lucide-react'
import { api, Settings, Provider, TestResult } from '../lib/api'

const STAGE_LABELS: Record<string, string> = {
  clarify: '对话理解', routing: '意图路由（简报/修改分流）', concept: '创意方案',
  storyboard: '讲稿分镜', factcheck: '事实校对', visuals: '视觉代码',
  ingest: '资料摘要（M6）',
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="block text-xs text-mute mb-1">{label}</span>
      {children}
    </label>
  )
}

function TestButton({ onTest }: { onTest: () => Promise<TestResult> }) {
  const [state, setState] = useState<'idle' | 'testing' | 'ok' | 'fail'>('idle')
  const [msg, setMsg] = useState('')
  const run = async () => {
    setState('testing'); setMsg('')
    try {
      const r = await onTest()
      if (r.ok) { setState('ok'); setMsg(`${r.latency_ms}ms${r.model_echo ? ` · ${r.model_echo}` : ''}`) }
      else { setState('fail'); setMsg(`${r.error?.kind}: ${r.error?.detail}`) }
    } catch (e: any) {
      setState('fail'); setMsg(String(e?.message || e))
    }
    setTimeout(() => { setState('idle'); setMsg('') }, 6000)
  }
  return (
    <span className="inline-flex items-center gap-1.5">
      <button onClick={run} disabled={state === 'testing'}
        className="h-9 px-3 rounded-lg border border-edge text-xs text-mute hover:text-accent hover:border-accent/50 disabled:opacity-50 transition-colors whitespace-nowrap">
        {state === 'testing' ? <Loader2 size={13} className="animate-spin inline" /> : '测试连接'}
      </button>
      {state === 'ok' && <span className="text-xs text-greenx inline-flex items-center gap-1"><CheckCircle2 size={13} />{msg}</span>}
      {state === 'fail' && <span className="text-xs text-red-400 inline-flex items-center gap-1 max-w-[260px] truncate" title={msg}><XCircle size={13} />{msg}</span>}
    </span>
  )
}

export default function SettingsModal({ onClose }: { onClose: () => void }) {
  const [s, setS] = useState<Settings | null>(null)
  const [saved, setSaved] = useState(false)
  const [showKeys, setShowKeys] = useState<Record<string, boolean>>({})
  const [confirmDiscard, setConfirmDiscard] = useState(false)
  const dirtyRef = useRef(false)
  const [dirty, setDirty] = useState(false)

  useEffect(() => { api.settings().then(setS) }, [])

  const tryClose = () => { if (dirtyRef.current) setConfirmDiscard(true); else onClose() }

  // Esc 关闭同样走未保存检查
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') tryClose() }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [])
  if (!s) return null

  const markDirty = (next: Settings) => {
    if (!dirtyRef.current) { dirtyRef.current = true; setDirty(true) }
    setS(next)
  }

  const save = async () => {
    await api.putSettings(s)
    dirtyRef.current = false; setDirty(false)
    setSaved(true); setTimeout(() => setSaved(false), 1500)
  }
  const updProvider = (id: string, patch: Partial<Provider>) =>
    markDirty({ ...s, providers: s.providers.map(p => p.id === id ? { ...p, ...patch } : p) })
  const addProvider = () =>
    markDirty({ ...s, providers: [...s.providers, { id: `p${Date.now()}`, name: '', type: 'openai', base_url: '', api_key: '', model: '' }] })
  const delProvider = (id: string) =>
    markDirty({ ...s, providers: s.providers.filter(p => p.id !== id) })
  const updVoice = (patch: Partial<Settings['voice']>) => markDirty({ ...s, voice: { ...s.voice, ...patch } })
  const updFeishu = (patch: Partial<Settings['feishu']>) => markDirty({ ...s, feishu: { ...s.feishu, ...patch } })

  return (
    <div className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4">
      <div className="w-full max-w-2xl max-h-[88vh] overflow-y-auto bg-panel border border-edge rounded-2xl" style={{ boxShadow: 'var(--shadow)' }}>
        <div className="flex items-center px-5 py-3.5 border-b border-edge sticky top-0 bg-panel z-10">
          <span className="font-semibold">设置</span>
          {dirty && <span className="ml-2 text-[11px] text-amberx">未保存</span>}
          <button onClick={tryClose} className="ml-auto p-1.5 text-mute hover:text-ink rounded-lg hover:bg-panel2"><X size={16} /></button>
        </div>

        <div className="p-5 space-y-7">
          {/* 供应商 */}
          <section>
            <div className="flex items-center mb-3">
              <h3 className="text-sm font-medium text-accent">模型供应商</h3>
              <button onClick={addProvider} className="ml-auto flex items-center gap-1 text-xs text-mute hover:text-accent"><Plus size={13} /> 添加</button>
            </div>
            <div className="space-y-3">
              {s.providers.map(p => (
                <div key={p.id} className="border border-edge rounded-xl p-4 bg-panel2/40 space-y-2.5">
                  <div className="grid grid-cols-[1fr_150px_auto] gap-2 items-end">
                    <Field label="名称">
                      <input value={p.name} onChange={e => updProvider(p.id, { name: e.target.value })} placeholder="如 Kimi / Opus" className="field-input" />
                    </Field>
                    <Field label="协议">
                      <select value={p.type} onChange={e => updProvider(p.id, { type: e.target.value })} className="field-input">
                        <option value="openai">OpenAI 兼容</option>
                        <option value="anthropic">Anthropic Messages</option>
                      </select>
                    </Field>
                    <button onClick={() => delProvider(p.id)} className="h-9 w-9 flex items-center justify-center rounded-lg text-mute hover:text-red-400 hover:bg-panel2" title="删除供应商"><Trash2 size={14} /></button>
                  </div>
                  <Field label="Base URL">
                    <input value={p.base_url} onChange={e => updProvider(p.id, { base_url: e.target.value })}
                      placeholder={p.type === 'anthropic' ? 'https://…/api' : 'https://api.moonshot.cn/v1'}
                      className="field-input mono" />
                  </Field>
                  <div className="grid grid-cols-[1fr_1fr] gap-2">
                    <Field label="API Key">
                      <div className="relative">
                        <input value={p.api_key} onChange={e => updProvider(p.id, { api_key: e.target.value })}
                          type={showKeys[p.id] ? 'text' : 'password'} placeholder="只存服务端，不回传明文"
                          className="field-input mono pr-9" />
                        <button onClick={() => setShowKeys(k => ({ ...k, [p.id]: !k[p.id] }))}
                          className="absolute right-2.5 top-1/2 -translate-y-1/2 text-mute hover:text-ink">
                          {showKeys[p.id] ? <EyeOff size={13} /> : <Eye size={13} />}
                        </button>
                      </div>
                    </Field>
                    <Field label="模型名">
                      <input value={p.model} onChange={e => updProvider(p.id, { model: e.target.value })} placeholder="如 kimi-k2.6" className="field-input mono" />
                    </Field>
                  </div>
                  <div className="pt-0.5">
                    <TestButton onTest={() => api.testProvider(p)} />
                  </div>
                </div>
              ))}
              {s.providers.length === 0 && <div className="text-xs text-mute">尚未添加供应商。添加后在下方为每个阶段选择模型。</div>}
            </div>
          </section>

          {/* 阶段模型分配 */}
          <section>
            <h3 className="text-sm font-medium text-accent mb-1">各阶段使用模型</h3>
            <p className="text-[11px] text-mute mb-3">意图路由 / 资料摘要未单独配置时，回退使用「对话理解」模型，再回退到任一供应商。</p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
              {Object.entries(STAGE_LABELS).map(([key, label]) => (
                <Field key={key} label={label}>
                  <select value={s.stage_models[key] || ''} onChange={e => markDirty({ ...s, stage_models: { ...s.stage_models, [key]: e.target.value || null } })}
                    className="field-input">
                    <option value="">未配置</option>
                    {s.providers.map(p => <option key={p.id} value={p.id}>{p.name || p.id} · {p.model}</option>)}
                  </select>
                </Field>
              ))}
            </div>
          </section>

          {/* 配音 */}
          <section>
            <h3 className="text-sm font-medium text-accent mb-3">配音（MiniMax）</h3>
            <div className="space-y-2.5">
              <div className="grid grid-cols-[1fr_120px] gap-2">
                <Field label="API Key">
                  <div className="relative">
                    <input value={s.voice.minimax_api_key} onChange={e => updVoice({ minimax_api_key: e.target.value })}
                      type={showKeys['mm'] ? 'text' : 'password'} placeholder="MiniMax API Key"
                      className="field-input mono pr-9" />
                    <button onClick={() => setShowKeys(k => ({ ...k, mm: !k.mm }))} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-mute hover:text-ink">
                      {showKeys['mm'] ? <EyeOff size={13} /> : <Eye size={13} />}
                    </button>
                  </div>
                </Field>
                <Field label="语速">
                  <input type="number" step="0.025" min="0.5" max="2" value={s.voice.speed}
                    onChange={e => updVoice({ speed: parseFloat(e.target.value) || 0.925 })}
                    className="field-input mono" />
                </Field>
              </div>
              <div className="grid grid-cols-2 gap-2">
                <Field label="音色 ID">
                  <input value={s.voice.voice_id} onChange={e => updVoice({ voice_id: e.target.value })}
                    placeholder="你的 MiniMax 音色 ID" className="field-input mono" />
                </Field>
                <Field label="TTS 模型">
                  <input value={s.voice.model} onChange={e => updVoice({ model: e.target.value })} className="field-input mono" />
                </Field>
              </div>
              <Field label="TTS Base URL">
                <input value={s.voice.minimax_base_url} onChange={e => updVoice({ minimax_base_url: e.target.value })}
                  placeholder="https://ai.ctaigw.cn/v1" className="field-input mono" />
              </Field>
              <div className="pt-0.5">
                <TestButton onTest={() => api.testMinimax({ api_key: s.voice.minimax_api_key, base_url: s.voice.minimax_base_url, model: s.voice.model, voice_id: s.voice.voice_id })} />
              </div>
            </div>
          </section>

          {/* 飞书集成 */}
          <section>
            <h3 className="text-sm font-medium text-accent mb-1">飞书集成（资料摄取）</h3>
            <p className="text-[11px] text-mute mb-3">自建应用的 App ID / Secret，用于读取飞书文档链接。应用需开通文档读取权限，且文档对该应用可见。</p>
            <div className="grid grid-cols-2 gap-2">
              <Field label="App ID">
                <input value={s.feishu.app_id} onChange={e => updFeishu({ app_id: e.target.value })}
                  placeholder="cli_…" className="field-input mono" />
              </Field>
              <Field label="App Secret">
                <div className="relative">
                  <input value={s.feishu.app_secret} onChange={e => updFeishu({ app_secret: e.target.value })}
                    type={showKeys['fs'] ? 'text' : 'password'} placeholder="只存服务端"
                    className="field-input mono pr-9" />
                  <button onClick={() => setShowKeys(k => ({ ...k, fs: !k.fs }))} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-mute hover:text-ink">
                    {showKeys['fs'] ? <EyeOff size={13} /> : <Eye size={13} />}
                  </button>
                </div>
              </Field>
            </div>
          </section>
        </div>

        <div className="flex items-center px-5 py-3.5 border-t border-edge sticky bottom-0 bg-panel">
          {saved && <span className="text-greenx text-xs inline-flex items-center gap-1"><CheckCircle2 size={13} />已保存</span>}
          <div className="ml-auto flex gap-2">
            <button onClick={tryClose} className="px-4 h-9 rounded-lg border border-edge text-sm text-mute hover:text-ink transition-colors">取消</button>
            <button onClick={save} className="px-4 h-9 rounded-lg bg-accent text-onaccent text-sm font-medium hover:brightness-110 transition-all">保存</button>
          </div>
        </div>
      </div>

      {/* 未保存确认 */}
      {confirmDiscard && (
        <div className="fixed inset-0 z-[60] bg-black/60 flex items-center justify-center p-4">
          <div className="w-full max-w-sm bg-panel border border-edge rounded-2xl p-5" style={{ boxShadow: 'var(--shadow)' }}>
            <div className="font-semibold mb-2">放弃未保存的修改？</div>
            <div className="text-sm text-mute mb-4">设置中有尚未保存的修改，关闭后将丢失。</div>
            <div className="flex justify-end gap-2">
              <button onClick={() => setConfirmDiscard(false)} className="px-4 h-9 rounded-lg border border-edge text-sm text-mute hover:text-ink">继续编辑</button>
              <button onClick={() => { setConfirmDiscard(false); onClose() }} className="px-4 h-9 rounded-lg bg-amberx/90 text-onaccent text-sm font-medium hover:brightness-110">放弃修改</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
