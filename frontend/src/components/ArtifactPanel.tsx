import { useEffect, useState } from 'react'
import { X, FileText, Code2, Images, Music4, Video, ListChecks, Activity, BookMarked, Link2, Loader2, Play, Square, SquarePen, Copy, ChevronDown } from 'lucide-react'
import { api, Artifact, Project, PipelineState, Annotation, Reference, StageProgress } from '../lib/api'
import { copyText } from '../lib/clipboard'
import { toastError, toastSuccess } from './Toast'
import VideoViewer from './VideoViewer'

const TYPE_ICON: Record<string, any> = { doc: FileText, code: Code2, gallery: Images, audio: Music4, video: Video, json: ListChecks }
// 原始响应展示上限（C-UX-07）：大文件只读前 64KB，避免浏览器卡死
const RAW_LIMIT = 64 * 1024

function RawResponseView({ pid }: { pid: string }) {
  const [state, setState] = useState<{
    loading: boolean; name?: string; text?: string; truncated?: boolean; error?: string; size?: number
  }>({ loading: true })
  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        const list = await api.rawResponses(pid)
        const latest = list.latest
        if (!latest) {
          if (alive) setState({ loading: false, error: '该项目还没有模型原始响应文件' })
          return
        }
        const r = await fetch(api.fileUrl(pid, `raw_responses/${latest.name}`))
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        const full = await r.text()
        if (!alive) return
        setState({ loading: false, name: latest.name, text: full.slice(0, RAW_LIMIT),
          truncated: full.length > RAW_LIMIT, size: latest.size })
      } catch (e) {
        if (alive) setState({ loading: false, error: e instanceof Error ? e.message : '读取失败' })
      }
    })()
    return () => { alive = false }
  }, [pid])

  if (state.loading) return <div className="mt-2 text-[11px] text-mute">正在读取最近原始响应…</div>
  if (state.error) return <div className="mt-2 text-[11px] text-mute">{state.error}</div>
  return (
    <div className="mt-2">
      <div className="flex items-center gap-2 text-[11px] text-mute mb-1">
        <span className="mono truncate">{state.name}</span>
        <span className="shrink-0">{Math.round((state.size || 0) / 1024)}KB</span>
        {state.truncated && <span className="shrink-0 text-amberx">（已截断至 64KB）</span>}
        <button onClick={async () => {
          const ok = await copyText(state.text || '')
          ok ? toastSuccess('原始响应已复制') : toastError('复制失败，请手动选中复制')
        }} className="ml-auto shrink-0 flex items-center gap-1 text-accent/80 hover:text-accent">
          <Copy size={11} /> 复制
        </button>
      </div>
      <pre className="mono text-[11px] leading-5 max-h-72 overflow-auto whitespace-pre-wrap break-words bg-base/60 rounded-lg p-2.5">{state.text}</pre>
    </div>
  )
}

function JsonBlock({ data }: { data: any }) {
  return <pre className="mono text-xs leading-5 bg-base/60 rounded-lg p-3 overflow-x-auto whitespace-pre-wrap">{JSON.stringify(data, null, 2)}</pre>
}

function DocView({ pid, art, onConfirmGate, pipeline }: { pid: string; art: Artifact; onConfirmGate: (sel?: number) => void; pipeline: PipelineState | null }) {
  const [data, setData] = useState<any>(null)
  useEffect(() => { fetch(api.fileUrl(pid, art.path)).then(r => r.json()).then(setData).catch(() => setData(null)) }, [pid, art.path])
  if (!data) return <div className="text-mute text-sm p-4">加载中…</div>

  if (art.logical_name === 'concept' && data.directions) {
    const awaiting = pipeline?.status === 'awaiting_gate' && pipeline.gate?.stage === 3
    return (
      <div className="p-4 space-y-3">
        <div className="text-xs text-mute">来源：{data._source || '未知'} · 选择一个方向后确认</div>
        {data.directions.map((d: any, i: number) => (
          <div key={i} className="border border-edge rounded-xl p-4 bg-panel2/50 hover:border-accent/40 transition-colors">
            <div className="font-semibold text-accent mb-1">{d.name}</div>
            <div className="text-sm text-ink/85 mb-2">{d.angle}</div>
            <ol className="text-xs text-mute space-y-0.5 mb-2">
              {(d.narrative || []).map((n: string, j: number) => <li key={j}>{j + 1}. {n}</li>)}
            </ol>
            <div className="text-xs text-mute">风格：{d.style} · 预计 {d.scene_count} 镜</div>
            {awaiting && (
              <button onClick={() => onConfirmGate(i)}
                className="mt-3 px-3 py-1.5 rounded-lg bg-accent text-onaccent text-xs font-medium hover:brightness-110">
                选这个方向，继续
              </button>
            )}
          </div>
        ))}
      </div>
    )
  }

  if ((art.logical_name === 'storyboard' || art.logical_name === 'storyboard_draft') && data.scenes) {
    return (
      <div className="p-4">
        <div className="text-xs text-mute mb-3">来源：{data._source || art.source} · {data.scenes.length} 镜{data.narrative_line ? ` · 主线：${data.narrative_line}` : ''}</div>
        <div className="space-y-3">
          {data.scenes.map((sc: any) => (
            <div key={sc.id} className="border border-edge rounded-xl p-3.5 bg-panel2/50">
              <div className="flex items-center gap-2 mb-1.5">
                <span className="mono text-xs text-accent">#{sc.id}</span>
                <span className="font-medium text-sm">{sc.title}</span>
                {sc.duration && <span className="ml-auto mono text-xs text-mute">{sc.duration}s</span>}
              </div>
              {sc.purpose && <div className="text-xs text-purplex mb-1">目的：{sc.purpose}</div>}
              {sc.visual && <div className="text-xs text-mute mb-1.5">画面：{sc.visual}</div>}
              <div className="text-sm text-ink/85 mb-1.5">{sc.narration}</div>
              {sc.tts && sc.tts !== sc.narration && <div className="text-xs text-mute">TTS：{sc.tts}</div>}
              {(sc.captions || []).map((c: string, j: number) => (
                <div key={j} className="mono text-xs text-greenx/90 mt-0.5">▸ {c}</div>
              ))}
            </div>
          ))}
        </div>
      </div>
    )
  }
  return <div className="p-4"><JsonBlock data={data} /></div>
}

function CodeView({ pid, art }: { pid: string; art: Artifact }) {
  const [code, setCode] = useState('')
  useEffect(() => { fetch(api.fileUrl(pid, art.path)).then(r => r.text()).then(setCode) }, [pid, art.path])
  return <pre className="mono text-xs leading-5 p-4 overflow-auto whitespace-pre">{code || '加载中…'}</pre>
}

function frameLabel(f: string): string {
  const m = f.match(/scene0*(\d+)_(start|mid|end)/)
  if (!m) return f
  const pos = { start: '开始', mid: '中段', end: '结束' }[m[2]] || m[2]
  return `第${parseInt(m[1])}镜·${pos}`
}

function GalleryView({ pid, art, onSend }: { pid: string; art: Artifact; onSend: (t: string) => void }) {
  const frames: string[] = art.extra?.frames || []
  const [editing, setEditing] = useState<string | null>(null)
  const [text, setText] = useState('')

  const submit = (f: string) => {
    const t = text.trim()
    if (!t) return
    onSend(`【关键帧修改 ${frameLabel(f)}（${f}）】${t}`)
    setEditing(null); setText('')
  }

  return (
    <div className="p-4">
      <div className="text-[11px] text-mute/80 mb-3 leading-5">
        对某帧不满意？点图下方的「修改这帧」写清要改的内容并发送，会只改视觉代码后重新渲染关键帧；
        也可以直接在对话里描述（如「第5帧的减号显示不出来」）。
      </div>
      <div className="grid grid-cols-2 gap-3">
        {frames.map(f => (
          <div key={f}>
            <a href={api.fileUrl(pid, `${art.path}/${f}`)} target="_blank" rel="noreferrer">
              <img src={api.fileUrl(pid, `${art.path}/${f}`)} alt={f} className="rounded-lg border border-edge hover:border-accent/50 transition-colors" />
            </a>
            <div className="flex items-center mt-1 gap-2">
              <span className="mono text-[10px] text-mute">{frameLabel(f)}</span>
              <button onClick={() => { setEditing(editing === f ? null : f); setText('') }}
                className="ml-auto flex items-center gap-1 text-[11px] text-accent/80 hover:text-accent">
                <SquarePen size={11} /> 修改这帧
              </button>
            </div>
            {editing === f && (
              <div className="mt-1.5 border border-edge rounded-lg p-2 bg-panel2/50">
                <textarea value={text} onChange={e => setText(e.target.value)} autoFocus rows={2}
                  onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(f) } }}
                  placeholder="要写改什么，如：减号显示为方框"
                  className="field-input w-full text-xs resize-none" />
                <div className="flex justify-end gap-1.5 mt-1.5">
                  <button onClick={() => { setEditing(null); setText('') }}
                    className="px-2 py-1 rounded-md text-[11px] text-mute hover:text-ink">取消</button>
                  <button onClick={() => submit(f)} disabled={!text.trim()}
                    className="px-2.5 py-1 rounded-md bg-accent text-onaccent text-[11px] font-medium hover:brightness-110 disabled:opacity-40">发送修改</button>
                </div>
              </div>
            )}
          </div>
        ))}
        {frames.length === 0 && <div className="text-mute text-sm col-span-2">无预览帧</div>}
      </div>
    </div>
  )
}

function AudioView({ pid, art }: { pid: string; art: Artifact }) {
  const [scenes, setScenes] = useState<any[]>([])
  useEffect(() => {
    fetch(api.fileUrl(pid, 'timeline.json')).then(r => r.json())
      .then(tl => setScenes(tl.scenes || [])).catch(() => {})
  }, [pid, art.id])
  return (
    <div className="p-4 space-y-3">
      <div className="text-xs text-mute">音色：{art.extra?.voice_id} · 语速 {art.extra?.speed}</div>
      {scenes.filter(s => s.audio_path).map(s => (
        <div key={s.id} className="border border-edge rounded-lg p-3 bg-panel2/50">
          <div className="text-xs mb-1.5">#{s.id} {s.title} <span className="mono text-mute ml-1">{s.audio_duration?.toFixed(1)}s</span></div>
          <audio controls preload="none" src={api.fileUrl(pid, s.audio_path)} className="w-full h-8" />
        </div>
      ))}
      {scenes.every(s => !s.audio_path) && <div className="text-mute text-sm">时间轴中暂无音频记录</div>}
    </div>
  )
}

function RefsView({ pid, onSend }: { pid: string; onSend: (t: string) => void }) {
  const [refs, setRefs] = useState<Reference[]>([])
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const load = () => api.projectRefs(pid).then(setRefs).catch(() => {})
  useEffect(() => { load() }, [pid])

  const add = async () => {
    const u = url.trim()
    if (!u || busy) return
    setBusy(true); setErr('')
    try {
      const ref = await api.ingest(u)
      await api.attachRef(pid, ref.id)
      setUrl(''); load()
    } catch (e: any) {
      setErr(`${e.kind ? `[${e.kind}] ` : ''}${e.message}`)
    }
    setBusy(false)
  }

  return (
    <div className="p-4 space-y-3">
      <div className="text-xs text-mute">项目参考资料：摄取的网页/飞书文档摘要会注入创意与分镜阶段的提示词。</div>
      <div className="flex gap-2">
        <input value={url} onChange={e => setUrl(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') add() }}
          placeholder="粘贴网页或飞书文档链接，回车摄取"
          className="field-input mono flex-1" />
        <button onClick={add} disabled={busy || !url.trim()}
          className="h-9 px-3 rounded-lg bg-accent text-onaccent text-xs font-medium hover:brightness-110 disabled:opacity-40 flex items-center gap-1">
          {busy ? <Loader2 size={13} className="animate-spin" /> : <Link2 size={13} />} 摄取
        </button>
      </div>
      {err && <div className="text-xs text-red-400 bg-red-400/10 rounded-lg px-3 py-2">{err}</div>}
      {refs.length === 0 && !err && <div className="text-mute text-sm">尚未关联资料。也可以在对话中直接发链接。</div>}
      {refs.map(r => (
        <div key={r.id} className="border border-edge rounded-xl p-3.5 bg-panel2/50">
          <div className="flex items-center gap-2 mb-1">
            <BookMarked size={13} className="text-accent shrink-0" />
            <span className="font-medium text-sm flex-1 min-w-0 truncate">{r.title}</span>
            <span className="text-[10px] text-mute shrink-0">{r.ingested_via === 'feishu' ? '飞书' : r.ingested_via === 'feishu_public' ? '飞书·公开页' : '网页'}</span>
          </div>
          <div className="text-sm text-ink/85 mb-1.5">{r.summary}</div>
          {r.key_points.slice(0, 5).map((k, i) => (
            <div key={i} className="text-xs text-mute">· {k}</div>
          ))}
          <a href={r.source_url} target="_blank" rel="noreferrer"
            className="mono text-[10px] text-accent/70 hover:text-accent break-all">{r.source_url}</a>
        </div>
      ))}
    </div>
  )
}

export default function ArtifactPanel({ project, pipeline, stageNames, artifacts, activeId, setActiveId, onClose, onSend, onConfirmGate, onRunStage, onStop, stageProgress }: {
  project: Project; pipeline: PipelineState | null; stageNames: Record<string, string>
  artifacts: Artifact[]; activeId: string | null; setActiveId: (id: string) => void
  onClose: () => void; onSend: (t: string, a?: Annotation[] | null) => void; onConfirmGate: (sel?: number) => void
  onRunStage: (stage: number) => void; onStop: () => void
  stageProgress?: Record<number, StageProgress>
}) {
  const [showRaw, setShowRaw] = useState(false)
  const active = artifacts.find(a => a.id === activeId)
    || [...artifacts].reverse().find(a => a.current)
  const showPipeline = activeId === '__pipeline' || (!active && activeId !== '__refs')
  const showRefs = activeId === '__refs'
  const versions = active ? artifacts.filter(a => a.logical_name === active.logical_name) : []

  return (
    <div className="h-full flex flex-col bg-panel">
      <div className="shrink-0 flex items-center gap-1 px-2 py-1.5 border-b border-edge overflow-x-auto">
        <button onClick={() => setActiveId('__pipeline')}
          className={`flex items-center gap-1 px-2.5 py-1.5 rounded-md text-xs whitespace-nowrap ${showPipeline ? 'bg-panel2 text-accent' : 'text-mute hover:text-ink'}`}>
          <Activity size={13} /> 流水线
        </button>
        <button onClick={() => setActiveId('__refs')}
          className={`flex items-center gap-1 px-2.5 py-1.5 rounded-md text-xs whitespace-nowrap ${showRefs ? 'bg-panel2 text-accent' : 'text-mute hover:text-ink'}`}>
          <BookMarked size={13} /> 资料
        </button>
        {[...artifacts].reverse().filter(a => a.current).map(a => {
          const Icon = TYPE_ICON[a.type] || FileText
          return (
            <button key={a.id} onClick={() => setActiveId(a.id)}
              className={`flex items-center gap-1 px-2.5 py-1.5 rounded-md text-xs whitespace-nowrap ${active?.id === a.id ? 'bg-panel2 text-accent' : 'text-mute hover:text-ink'}`}>
              <Icon size={13} /> {a.title}
            </button>
          )
        })}
        <div className="flex-1" />
        <button onClick={onClose} className="p-1.5 text-mute hover:text-ink"><X size={15} /></button>
      </div>

      <div className="flex-1 overflow-y-auto min-h-0">
        {showRefs ? (
          <RefsView pid={project.id} onSend={onSend} />
        ) : showPipeline ? (
          <div className="p-4">
            <div className="flex items-center mb-1">
              <div className="text-xs text-mute">档位：{pipeline?.tier === 'concept' ? '创意档' : pipeline?.tier === 'storyboard' ? '分镜档' : pipeline?.tier === 'video' ? '成片档' : '未开始'}</div>
              <div className="flex-1" />
              {pipeline?.status === 'running' && (
                <button onClick={onStop}
                  className="flex items-center gap-1 text-xs px-2 py-1 rounded-md bg-red-400/15 text-red-400 hover:bg-red-400/25">
                  <Square size={11} /> 中断
                </button>
              )}
            </div>
            <div className="text-[11px] text-mute/70 mb-3">点击任意节点可直接执行该阶段；门禁阶段执行后需确认。</div>
            {[2, 3, 4, 5, 6, 7, 8, 9, 10, 11].map(s => {
              const running = pipeline?.status === 'running'
              const done = (pipeline?.stage || 1) > s || pipeline?.status === 'tier_done'
                || pipeline?.status === 'finished'
                || (pipeline?.status === 'awaiting_gate' && (pipeline?.stage || 0) >= s)
              const cur = pipeline?.stage === s && running
              const isGate = [3, 5, 7, 11].includes(s)
              const sp = cur ? stageProgress?.[s] : undefined
              return (
                <div key={s}>
                  <div
                    onClick={() => { if (!running) onRunStage(s) }}
                    title={running ? '运行中，先中断才能执行其他节点' : `点击执行「${stageNames[s]}」`}
                    className={`group flex items-center gap-2.5 py-1.5 px-1.5 -mx-1.5 rounded-lg transition-colors ${running ? 'cursor-not-allowed' : 'cursor-pointer hover:bg-panel2'}`}>
                    <span className={`w-2 h-2 rounded-full shrink-0 ${done ? 'bg-greenx' : cur ? 'bg-accent animate-pulse' : 'bg-edge'}`} />
                    <span className={`text-sm ${done ? 'text-ink/80' : cur ? 'text-accent' : 'text-mute'} ${!running && 'group-hover:text-ink'}`}>{stageNames[s]}</span>
                    {isGate && <span className="text-[10px] text-amberx/80 border border-amberx/30 rounded px-1">门禁</span>}
                    {pipeline?.status === 'awaiting_gate' && pipeline.gate?.stage === s && <span className="text-[10px] text-amberx">← 待确认</span>}
                    {cur && sp?.percent != null && <span className="mono text-[11px] text-accent">{sp.percent}%</span>}
                    {cur && <Loader2 size={12} className="text-accent animate-spin" />}
                    {!running && <Play size={11} className="text-mute/0 group-hover:text-accent transition-colors" />}
                  </div>
                  {cur && sp && (
                    <div className="pl-5 pr-2 pb-1.5">
                      {sp.percent != null && (
                        <div className="h-1 rounded-full bg-edge overflow-hidden">
                          <div className="h-full bg-accent transition-all duration-300" style={{ width: `${sp.percent}%` }} />
                        </div>
                      )}
                      {sp.detail && <div className="mt-1 text-[11px] text-mute truncate" title={sp.detail}>{sp.detail}</div>}
                    </div>
                  )}
                </div>
              )
            })}
            {pipeline?.error && (
              <div className="mt-3 text-xs text-red-300 bg-red-400/10 rounded-lg p-3">
                <div className="whitespace-pre-wrap break-words">{pipeline.error}</div>
                <div className="flex flex-wrap items-center gap-2 mt-2">
                  <button onClick={async () => {
                    const ok = await copyText(pipeline.error || '')
                    ok ? toastSuccess('错误信息已复制') : toastError('复制失败，请手动选中复制')
                  }}
                    className="flex items-center gap-1 px-2 py-1 rounded-md border border-red-400/30 text-red-300 hover:bg-red-400/15">
                    <Copy size={11} /> 复制错误
                  </button>
                  <button onClick={() => setShowRaw(v => !v)}
                    className="flex items-center gap-1 px-2 py-1 rounded-md border border-edge text-mute hover:text-ink">
                    <ChevronDown size={11} className={`transition-transform ${showRaw ? 'rotate-180' : ''}`} />
                    {showRaw ? '收起原始响应' : '查看最近原始响应'}
                  </button>
                </div>
                {showRaw && <RawResponseView pid={project.id} />}
              </div>
            )}
          </div>
        ) : active?.type === 'video' ? (
          <VideoViewer project={project} artifact={active} onSend={onSend} />
        ) : active?.type === 'doc' || active?.type === 'json' ? (
          <DocView pid={project.id} art={active} onConfirmGate={onConfirmGate} pipeline={pipeline} />
        ) : active?.type === 'code' ? (
          <CodeView pid={project.id} art={active} />
        ) : active?.type === 'gallery' ? (
          <GalleryView pid={project.id} art={active} onSend={onSend} />
        ) : active?.type === 'audio' ? (
          <AudioView pid={project.id} art={active} />
        ) : null}
      </div>

      {active && versions.length > 1 && (
        <div className="shrink-0 border-t border-edge px-3 py-2 flex items-center gap-2 text-xs text-mute">
          版本：
          {versions.map(v => (
            <button key={v.id} onClick={() => setActiveId(v.id)}
              className={`mono px-1.5 py-0.5 rounded ${v.id === active.id ? 'bg-accent/20 text-accent' : 'hover:text-ink'}`}>
              v{v.version}{v.current ? '●' : ''}
            </button>
          ))}
          <span className="ml-auto">来源：{active.source}</span>
        </div>
      )}
    </div>
  )
}
