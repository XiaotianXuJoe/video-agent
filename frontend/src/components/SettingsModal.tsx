import { useEffect, useRef, useState } from 'react'
import { Plus, Trash2, X, Eye, EyeOff, Loader2, CheckCircle2, XCircle } from 'lucide-react'
import { api, setToken, Settings, Provider, TestResult } from '../lib/api'

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

function TestButton({ onTest, disabled, hint }: {
  onTest: () => Promise<TestResult>; disabled?: boolean; hint?: string
}) {
  const [state, setState] = useState<'idle' | 'testing' | 'ok' | 'fail'>('idle')
  const [msg, setMsg] = useState('')
  const run = async () => {
    if (disabled) return
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
      <button onClick={run} disabled={state === 'testing' || disabled}
        title={disabled ? hint : undefined}
        className="h-9 px-3 rounded-lg border border-edge text-xs text-mute hover:text-accent hover:border-accent/50 disabled:opacity-50 disabled:cursor-not-allowed transition-colors whitespace-nowrap">
        {state === 'testing' ? <Loader2 size={13} className="animate-spin inline" /> : '测试连接'}
      </button>
      {disabled && hint && <span className="text-[11px] text-mute">{hint}</span>}
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
  // 已保存的供应商 id：测试连接只允许针对已保存配置（C-SEC-02）
  const [savedProviderIds, setSavedProviderIds] = useState<Set<string>>(new Set())
  const initialToken = useRef('')

  useEffect(() => {
    api.settings().then(x => {
      setS(x)
      setSavedProviderIds(new Set(x.providers.map(p => p.id)))
      initialToken.current = x.auth?.token || ''
    })
  }, [])

  const tryClose = () => { if (dirtyRef.current) setConfirmDiscard(true); else onClose() }

  // Esc 关闭同样走未保存检查
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') tryClose() }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [])
  // 注意：所有 hooks 必须在此之上——下面的提前 return 之后不允许再出现 hooks（React #310）
  const [bgmUploading, setBgmUploading] = useState(false)
  const [bgmError, setBgmError] = useState('')
  if (!s) return null

  const markDirty = (next: Settings) => {
    if (!dirtyRef.current) { dirtyRef.current = true; setDirty(true) }
    setS(next)
  }

  const save = async () => {
    const pw = (s.auth?.token || '').trim()
    const pwChanged = pw !== '' && pw !== '********' && pw !== initialToken.current
    const resp = await api.putSettings(s) as Settings
    // 用服务端回写的脱敏值刷新：已保存的供应商/Key 立即可测，且不会把明文留在表单里
    setS(resp)
    setSavedProviderIds(new Set(resp.providers.map(p => p.id)))
    initialToken.current = resp.auth?.token || ''
    dirtyRef.current = false; setDirty(false)
    if (pwChanged) {
      // 密码（token）已变更：旧 cookie 立即失效。用新 token 登出以清 cookie + 本地 token，
      // 整页刷新后重新走登录页（最简单也最不容易留下半登录状态）。
      setToken(pw)
      await api.logout()
      window.location.reload()
      return
    }
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
  const updAuth = (patch: Partial<Settings['auth']>) => markDirty({ ...s, auth: { ...s.auth, ...patch } })
  const updSecurity = (patch: Partial<Settings['security']>) => markDirty({ ...s, security: { ...s.security, ...patch } })
  const updContentRules = (patch: Partial<Settings['content_rules']>) =>
    markDirty({ ...s, content_rules: { ...(s.content_rules || {}), ...patch } as Settings['content_rules'] })
  const updPipeline = (patch: Partial<Settings['pipeline']>) =>
    markDirty({ ...s, pipeline: { ...(s.pipeline || { tts_chars_per_second: 5.6 }), ...patch } })
  const updBgm = (patch: Partial<Settings['bgm']>) =>
    markDirty({ ...s, bgm: { ...(s.bgm || { enabled: true, volume: 0.08, path: 'bgm.mp3' }), ...patch } })

  const onUploadBgm = async (file: File | null) => {
    if (!file) return
    setBgmUploading(true); setBgmError('')
    try {
      const r = await api.uploadBgm(file)
      updBgm({ path: r.path, enabled: true })
    } catch (e: any) {
      setBgmError(String(e?.message || e))
    } finally {
      setBgmUploading(false)
    }
  }

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
                    <TestButton onTest={() => api.testProvider(p.id)}
                      disabled={!savedProviderIds.has(p.id)} hint="保存后可测试（只用已存配置）" />
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

          {/* 创作规范 */}
          <section>
            <h3 className="text-sm font-medium text-accent mb-1">创作规范</h3>
            <p className="text-[11px] text-mute mb-3 leading-5">
              这些规则会注入讲稿分镜与事实校对的提示词，逐条可关；关闭后该条不再要求模型遵守。
            </p>
            <div className="space-y-2">
              {([
                ['huawei_od', '华为 OD 真题开篇点明「华为 OD + 考试日期 + 真题」'],
                ['no_suspense_opening', '第一镜不做认知冲突式悬念开场'],
                ['no_repeat_example', '相邻分镜不重复讲解同一个例子'],
                ['complexity_ending', '结尾直接以复杂度收束，不做易错点盘点'],
                ['integer_time', '画面上的时间读数一律整数'],
              ] as [keyof Settings['content_rules'], string][]).map(([key, label]) => (
                <label key={key} className="flex items-center gap-2.5 text-sm cursor-pointer">
                  <input type="checkbox"
                    checked={s.content_rules?.[key] ?? true}
                    onChange={e => updContentRules({ [key]: e.target.checked } as Partial<Settings['content_rules']>)} />
                  <span>{label}</span>
                </label>
              ))}
            </div>
            <div className="grid grid-cols-[180px_1fr] gap-2 items-end mt-3">
              <Field label="估算语速（字/秒）">
                <input type="number" step="0.1" min="3" max="8"
                  value={s.pipeline?.tts_chars_per_second ?? 5.6}
                  onChange={e => updPipeline({ tts_chars_per_second: parseFloat(e.target.value) || 5.6 })}
                  className="field-input mono" />
              </Field>
              <p className="text-[11px] text-mute leading-5">
                关键帧预览阶段按此语速估算镜头时长，默认 5.6（skill 实测 0.925 倍速）。
              </p>
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
                <TestButton onTest={() => api.testMinimax()}
                  disabled={!s.voice.has_minimax_key} hint="测试只用已保存配置，先保存 Key" />
              </div>
            </div>
          </section>

          {/* 背景音乐 */}
          <section>
            <h3 className="text-sm font-medium text-accent mb-1">背景音乐（BGM）</h3>
            <p className="text-[11px] text-mute mb-3 leading-5">
              低音量衬底，循环覆盖全片，混音时不会被压人声。文件放在服务端 assets 目录；
              找不到文件时该功能自动关闭，并在 QC 报告里说明。
            </p>
            <div className="space-y-2.5">
              <label className="flex items-center gap-2.5 text-sm cursor-pointer">
                <input type="checkbox" checked={s.bgm?.enabled ?? true}
                  onChange={e => updBgm({ enabled: e.target.checked })} />
                <span>启用背景音乐</span>
              </label>
              <div className="grid grid-cols-2 gap-2">
                <Field label="衬底音量（0-1）">
                  <input type="number" step="0.01" min="0.01" max="1"
                    value={s.bgm?.volume ?? 0.08}
                    onChange={e => updBgm({ volume: parseFloat(e.target.value) || 0.08 })}
                    className="field-input mono" />
                </Field>
                <Field label="BGM 文件（assets 目录内）">
                  <input value={s.bgm?.path ?? 'bgm.mp3'}
                    onChange={e => updBgm({ path: e.target.value })}
                    placeholder="bgm.mp3" className="field-input mono" />
                </Field>
              </div>
              <div className="flex items-center gap-3">
                <label className={`h-9 px-3 rounded-lg border border-edge text-xs inline-flex items-center gap-1.5 cursor-pointer hover:text-accent hover:border-accent/50 transition-colors ${bgmUploading ? 'opacity-60 pointer-events-none' : 'text-mute'}`}>
                  {bgmUploading ? <Loader2 size={13} className="animate-spin" /> : null}
                  {bgmUploading ? '上传中…' : '上传自定义 BGM'}
                  <input type="file" accept=".mp3,.wav,.m4a,.aac,.flac,.ogg,.opus" className="hidden"
                    onChange={e => { onUploadBgm(e.target.files?.[0] || null); e.currentTarget.value = '' }} />
                </label>
                <span className="text-[11px] text-mute">支持 mp3 / wav / m4a / flac 等，≤20MB，上传后记得保存</span>
              </div>
              {bgmError && <div className="text-xs text-red-400">上传失败：{bgmError}</div>}
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
          {/* 访问密码 */}
          <section>
            <h3 className="text-sm font-medium text-accent mb-1">访问密码（全站鉴权）</h3>
            <p className="text-[11px] text-mute mb-3 leading-5">
              设置后所有 API 与产物文件都需要登录才能访问。留空表示不修改当前密码；修改保存后会立即退出登录并需要重新登录。
              纯 HTTP 环境不会加 Secure 标记，请勿在公网明文 HTTP 上长期沿用同一密码。
            </p>
            <Field label="访问密码">
              <input value={s.auth?.token || ''} onChange={e => updAuth({ token: e.target.value })}
                type="password" placeholder={s.auth?.has_token ? '已设置（留空保持）' : '设置密码后所有访问都需登录'}
                className="field-input mono" autoComplete="new-password" />
            </Field>
          </section>

          {/* 执行安全 */}
          <section>
            <h3 className="text-sm font-medium text-accent mb-2">执行安全</h3>
            <label className="flex items-start gap-2.5 border border-amberx/25 bg-amberx/5 rounded-xl p-3 cursor-pointer">
              <input type="checkbox" className="mt-0.5"
                checked={!!s.security?.allow_generated_code_exec}
                onChange={e => updSecurity({ allow_generated_code_exec: e.target.checked })} />
              <span>
                <span className="block text-sm">允许执行生成的代码（阶段 6 / 7 / 10）</span>
                <span className="block text-[11px] text-amberx mt-0.5 leading-5">
                  风险提示：开启后，模型生成的 visuals.py 会在服务器本机以工具子进程执行（渲染关键帧与成片）。
                  关闭后这三阶段会直接失败。仅建议在单人、受信任的环境开启。
                </span>
              </span>
            </label>
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
