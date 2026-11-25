import { useCallback, useEffect, useRef, useState } from 'react'
import { Clapperboard, Plus, Settings, Trash2, MessageSquare, PanelLeftClose, PanelLeftOpen, Sun, Moon, Pencil, Check, Activity, X } from 'lucide-react'
import { api, streamChat, Session, Message, Project, PipelineState, Artifact, Chip, Annotation } from './lib/api'
import ChatView from './components/ChatView'
import ArtifactPanel from './components/ArtifactPanel'
import SettingsModal from './components/SettingsModal'

export interface LiveMsg { role: string; text: string; streaming?: boolean }

type Theme = 'dark' | 'light'

function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(() => {
    const saved = localStorage.getItem('va-theme')
    if (saved === 'light' || saved === 'dark') return saved
    return window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark'
  })
  useEffect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem('va-theme', theme)
  }, [theme])
  return [theme, () => setTheme(t => t === 'dark' ? 'light' : 'dark')]
}

function timeGroup(updated: number): string {
  const d = new Date(updated * 1000)
  const now = new Date()
  const startOfDay = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const diff = startOfDay(now) - startOfDay(d)
  if (diff <= 0) return '今天'
  if (diff <= 86400000) return '昨天'
  if (diff <= 7 * 86400000) return '最近 7 天'
  return '更早'
}
const GROUP_ORDER = ['今天', '昨天', '最近 7 天', '更早']

const STAGE_LIST: [number, string][] = [
  [2, '需求澄清'], [3, '创意方案'], [4, '讲稿分镜'], [5, '事实校对'], [6, '视觉代码'],
  [7, '关键帧预览'], [8, '配音合成'], [9, '时间轴组装'], [10, '渲染合成'], [11, 'QC 验收'],
]
const GATE_STAGES = new Set([3, 5, 7, 11])

/** 无项目时的流水线占位面板：保证流水线栏常开，新对话也能看到阶段结构 */
function EmptyPipelinePanel({ onClose }: { onClose: () => void }) {
  return (
    <div className="h-full flex flex-col bg-panel">
      <div className="shrink-0 flex items-center gap-1 px-2 py-1.5 border-b border-edge">
        <span className="flex items-center gap-1 px-2.5 py-1.5 rounded-md text-xs bg-panel2 text-accent">
          <Activity size={13} /> 流水线
        </span>
        <div className="flex-1" />
        <button onClick={onClose} className="p-1.5 text-mute hover:text-ink"><X size={15} /></button>
      </div>
      <div className="flex-1 overflow-y-auto p-4">
        <div className="text-xs text-mute mb-3 leading-5">
          还没有项目。发送算法题或讲解主题后会自动建立项目；之后每个阶段的执行进度都会实时显示在这里，也可以点击任意节点单独执行。
        </div>
        {STAGE_LIST.map(([no, name]) => (
          <div key={no} className="flex items-center gap-2.5 py-1.5 px-1.5">
            <span className="w-2 h-2 rounded-full shrink-0 bg-edge" />
            <span className="text-sm text-mute">{name}</span>
            {GATE_STAGES.has(no) && <span className="text-[10px] text-amberx/60 border border-amberx/20 rounded px-1">门禁</span>}
          </div>
        ))}
      </div>
    </div>
  )
}

function SessionItem({ s, active, projectTitle, onLoad, onDelete, onRename }: {
  s: Session; active: boolean; projectTitle?: string
  onLoad: () => void; onDelete: () => void; onRename: (t: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(s.title)
  const commit = () => {
    const t = draft.trim()
    setEditing(false)
    if (t && t !== s.title) onRename(t)
    else setDraft(s.title)
  }
  return (
    <div onClick={onLoad}
      className={`group flex items-center gap-2 px-2.5 py-2 rounded-lg text-[13px] cursor-pointer transition-colors ${active ? 'bg-panel2 text-accent' : 'text-ink/75 hover:bg-panel2/60'}`}>
      <MessageSquare size={13} className="shrink-0 opacity-60" />
      {editing ? (
        <input autoFocus value={draft} onChange={e => setDraft(e.target.value)}
          onClick={e => e.stopPropagation()} onBlur={commit}
          onKeyDown={e => { if (e.key === 'Enter') commit(); if (e.key === 'Escape') { setDraft(s.title); setEditing(false) } }}
          className="flex-1 min-w-0 bg-base border border-accent/50 rounded px-1.5 py-0.5 text-[13px] outline-none" />
      ) : (
        <span className="flex-1 min-w-0 truncate">{s.title || projectTitle || '新对话'}</span>
      )}
      <span className="shrink-0 hidden group-hover:flex items-center gap-0.5">
        {editing
          ? <button onClick={e => { e.stopPropagation(); commit() }} className="p-1 text-accent"><Check size={12} /></button>
          : <button onClick={e => { e.stopPropagation(); setDraft(s.title); setEditing(true) }} className="p-1 text-mute hover:text-accent" title="重命名"><Pencil size={12} /></button>}
        <button onClick={e => { e.stopPropagation(); onDelete() }} className="p-1 text-mute hover:text-amberx" title="删除"><Trash2 size={12} /></button>
      </span>
    </div>
  )
}

export default function App() {
  const [theme, toggleTheme] = useTheme()
  const [sessions, setSessions] = useState<Session[]>([])
  const [sid, setSid] = useState<string>('')
  const [history, setHistory] = useState<Message[]>([])
  const [live, setLive] = useState<LiveMsg | null>(null)
  const [chips, setChips] = useState<Chip[]>([])
  const [project, setProject] = useState<Project | null>(null)
  const [pipeline, setPipeline] = useState<PipelineState | null>(null)
  const [stageNames, setStageNames] = useState<Record<string, string>>({})
  const [artifacts, setArtifacts] = useState<Artifact[]>([])
  const [panelOpen, setPanelOpen] = useState(true)
  const [activeArtifact, setActiveArtifact] = useState<string | null>(null)
  const [showSettings, setShowSettings] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [busy, setBusy] = useState(false)
  const sessionRef = useRef(sid)
  sessionRef.current = sid
  // 流式回调是闭包，必须经 ref 取「当前项目」，否则新项目创建后
  // onStage/onGate/onDone 读到的仍是旧的 null，流水线状态永远不刷新
  const projectRef = useRef<string | null>(null)

  const refreshProject = useCallback(async (pid: string) => {
    projectRef.current = pid
    const d = await api.project(pid)
    setProject(d.project); setPipeline(d.pipeline)
    setStageNames(d.stage_names); setChips(d.chips)
    setArtifacts(await api.artifacts(pid))
  }, [])

  const loadSession = useCallback(async (id: string) => {
    setSid(id)
    const msgs = await api.messages(id)
    setHistory(msgs); setLive(null)
    const s = (await api.sessions()).find(x => x.id === id)
    if (s?.project_id) {
      await refreshProject(s.project_id)
      // 打开带项目的会话时自动展开流水线面板
      setPanelOpen(true); setActiveArtifact('__pipeline')
    }
    else {
      projectRef.current = null; setProject(null); setPipeline(null); setArtifacts([]); setChips([])
      // 流水线栏常开：无项目的会话也展开占位面板
      setPanelOpen(true); setActiveArtifact('__pipeline')
    }
  }, [refreshProject])

  useEffect(() => {
    (async () => {
      const ss = await api.sessions()
      if (ss.length === 0) {
        const s = await api.newSession()
        setSessions([s]); await loadSession(s.id)
      } else {
        setSessions(ss); await loadSession(ss[0].id)
      }
    })()
  }, [loadSession])

  const newChat = async () => {
    const s = await api.newSession()
    setSessions(await api.sessions())
    await loadSession(s.id)
  }

  const renameSession = async (id: string, title: string) => {
    await api.renameSession(id, title)
    setSessions(await api.sessions())
  }

  const deleteSession = async (id: string) => {
    await api.delSession(id)
    let ss = await api.sessions()
    if (ss.length === 0) { const s = await api.newSession(); ss = [s] }
    setSessions(ss)
    if (id === sid) await loadSession(ss[0].id)
  }

  const send = useCallback(async (text: string, attachments: Annotation[] | null = null) => {
    if (busy || !sessionRef.current) return
    setBusy(true)
    setHistory(h => [...h, { id: `u${Date.now()}`, role: 'user', content: text, kind: attachments ? 'annotation' : 'text', payload: null, created: Date.now() / 1000 }])
    setLive({ role: 'assistant', text: '', streaming: true })
    await streamChat(sessionRef.current, text, attachments, {
      onToken: t => setLive(l => l ? { ...l, text: l.text + t } : l),
      onArtifact: a => {
        setArtifacts(prev => {
          const next = prev.filter(x => !(x.logical_name === a.logical_name && x.current))
          return [...next.map(x => x.logical_name === a.logical_name ? { ...x, current: false } : x), a]
        })
        setActiveArtifact(a.id); setPanelOpen(true)
        if (projectRef.current) api.artifacts(projectRef.current).then(setArtifacts)
      },
      onProject: d => {
        projectRef.current = d.project.id
        setProject(d.project); refreshProject(d.project.id)
        setPanelOpen(true); setActiveArtifact('__pipeline')
      },
      onStage: () => { const pid = projectRef.current; if (pid) refreshProject(pid) },
      onGate: () => { const pid = projectRef.current; if (pid) refreshProject(pid) },
      onSuggestions: c => setChips(c),
      onSessionRenamed: d => {
        setSessions(prev => prev.map(s => s.id === d.session_id ? { ...s, title: d.title } : s))
      },
      onReference: () => {
        const pid = projectRef.current
        if (pid) refreshProject(pid)
      },
      onFrontendAction: a => {
        if (a.action === 'open_settings') setShowSettings(true)
        if (a.action === 'open_artifact') {
          const art = artifacts.find(x => x.logical_name === a.artifact && x.current)
            || artifacts.find(x => x.logical_name === a.artifact)
          if (art) { setActiveArtifact(art.id); setPanelOpen(true) }
        }
        if (a.action === 'open_refs') { setPanelOpen(true); setActiveArtifact('__refs') }
        if (a.action === 'new_chat') newChat()
      },
      onError: e => setLive(l => ({ role: 'assistant', text: (l?.text || '') + `\n\n❌ ${e.message || '出错了'}` })),
      onDone: () => {
        setLive(l => {
          if (l) setHistory(h => [...h, { id: `a${Date.now()}`, role: 'assistant', content: l.text, kind: 'text', payload: null, created: Date.now() / 1000 }])
          return null
        })
        setBusy(false)
        const pid = projectRef.current
        if (pid) refreshProject(pid)
      },
    })
    setBusy(false)
  }, [busy, artifacts, refreshProject])

  const chipClick = (c: Chip) => {
    if (c.action === 'free_text') return // 仅提示用户输入
    if (c.action === 'open_settings') { setShowSettings(true); return }
    if (c.action === 'open_artifact') {
      const art = artifacts.find(x => x.logical_name === c.artifact && x.current)
        || artifacts.find(x => x.logical_name === c.artifact)
      if (art) { setActiveArtifact(art.id); setPanelOpen(true) }
      return
    }
    if (c.action === 'open_refs') { setPanelOpen(true); setActiveArtifact('__refs'); return }
    if (c.action === 'new_chat') { newChat(); return }
    if (c.action === 'show_pipeline') { setPanelOpen(true); setActiveArtifact('__pipeline'); return }
    if (c.action === 'show_error') { send(`当前失败详情是什么？`); return }
    if (c.action === 'start_chat') { send(c.hint === '发给我题目和正确代码，我们开始' ? '我想制作一个算法讲解视频' : '我想制作一个科普讲解视频'); return }
    send(`__action:${JSON.stringify(c)}`)
  }

  const confirmGate = (selected?: number) => {
    send(`__action:${JSON.stringify({ action: 'confirm_gate', selected_direction: selected ?? null })}`)
  }

  // 流水线运行期间轮询刷新：面板发起的单点执行没有对话流事件，靠轮询保证实时可见
  useEffect(() => {
    if (pipeline?.status !== 'running') return
    const timer = setInterval(() => {
      const pid = projectRef.current
      if (pid) refreshProject(pid)
    }, 2000)
    return () => clearInterval(timer)
  }, [pipeline?.status, refreshProject])

  const runStage = async (stage: number) => {
    const pid = projectRef.current
    if (!pid) return
    try {
      await api.runStage(pid, stage)
      setPipeline(pl => pl ? { ...pl, status: 'running', stage } : pl)
      setTimeout(() => { if (projectRef.current) refreshProject(projectRef.current) }, 300)
    } catch (e) {
      alert(e instanceof Error ? e.message : '启动失败')
    }
  }

  const stopProject = async () => {
    const pid = projectRef.current
    if (!pid) return
    await api.stopProject(pid)
    setTimeout(() => { if (projectRef.current) refreshProject(projectRef.current) }, 600)
  }

  // 对话栏 / 流水线栏分隔条拖动：右侧面板宽度持久化到 localStorage
  const PANEL_W_DEFAULT = 520
  const [panelW, setPanelW] = useState<number>(() => {
    const v = Number(localStorage.getItem('va-panel-w'))
    return v >= 320 ? v : PANEL_W_DEFAULT
  })
  const dragRef = useRef<{ startX: number; startW: number } | null>(null)

  const clampPanelW = (w: number) => {
    const max = Math.min(window.innerWidth - 480, Math.round(window.innerWidth * 0.8))
    return Math.round(Math.min(max, Math.max(320, w)))
  }

  const onSplitterDown = (e: React.PointerEvent) => {
    e.preventDefault()
    dragRef.current = { startX: e.clientX, startW: panelW }
    const move = (ev: PointerEvent) => {
      const d = dragRef.current
      if (!d) return
      setPanelW(clampPanelW(d.startW + (d.startX - ev.clientX)))
    }
    const up = () => {
      dragRef.current = null
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      setPanelW(w => { localStorage.setItem('va-panel-w', String(w)); return w })
    }
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }

  // 窗口收窄时同步钳制面板宽度，避免挤压对话区
  useEffect(() => {
    const onResize = () => setPanelW(w => clampPanelW(w))
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  const groups = GROUP_ORDER.map(g => ({ name: g, items: sessions.filter(s => timeGroup(s.updated) === g) })).filter(g => g.items.length > 0)

  return (
    <div className="h-full flex min-w-0">
      {/* 会话侧栏 */}
      {sidebarOpen && (
        <aside className="w-60 shrink-0 flex flex-col border-r border-edge bg-panel">
          <div className="flex items-center gap-2 px-3.5 h-13 py-3">
            <Clapperboard size={17} className="text-accent shrink-0" />
            <span className="font-semibold tracking-wide text-[15px]">讲解视频 Agent</span>
          </div>
          <div className="px-2.5 pb-2">
            <button onClick={newChat}
              className="w-full flex items-center justify-center gap-1.5 h-9 rounded-lg border border-edge text-[13px] text-ink/80 hover:border-accent/50 hover:text-accent transition-colors">
              <Plus size={14} /> 新对话
            </button>
          </div>
          <div className="flex-1 overflow-y-auto px-2.5 pb-3">
            {groups.map(g => (
              <div key={g.name} className="mt-3 first:mt-1">
                <div className="px-2 pb-1 text-[11px] text-mute/70">{g.name}</div>
                {g.items.map(s => (
                  <SessionItem key={s.id} s={s} active={s.id === sid}
                    projectTitle={s.id === sid ? project?.title : undefined}
                    onLoad={() => loadSession(s.id)}
                    onDelete={() => deleteSession(s.id)}
                    onRename={t => renameSession(s.id, t)} />
                ))}
              </div>
            ))}
          </div>
        </aside>
      )}

      {/* 主区域 */}
      <div className="flex-1 flex flex-col min-w-0">
        <header className="glass-bar shrink-0 flex items-center gap-2 px-3 h-12 border-b border-edge relative z-10">
          <button onClick={() => setSidebarOpen(o => !o)} className="p-2 rounded-lg text-mute hover:text-ink hover:bg-panel2" title={sidebarOpen ? '收起侧栏' : '展开侧栏'}>
            {sidebarOpen ? <PanelLeftClose size={16} /> : <PanelLeftOpen size={16} />}
          </button>
          {!sidebarOpen && (
            <span className="flex items-center gap-1.5 font-semibold text-sm mr-1">
              <Clapperboard size={15} className="text-accent" /> 讲解视频 Agent
            </span>
          )}
          <span className="text-sm text-mute truncate">{sessions.find(s => s.id === sid)?.title}</span>
          <div className="flex-1" />
          {pipeline && (
            <span className={`text-xs px-2 py-0.5 rounded-full ${pipeline.status === 'awaiting_gate' ? 'bg-amberx/15 text-amberx' : pipeline.status === 'running' ? 'bg-accent/15 text-accent' : pipeline.status === 'failed' ? 'bg-red-400/15 text-red-400' : pipeline.status === 'paused' ? 'bg-mute/15 text-mute' : 'bg-greenx/15 text-greenx'}`}>
              {pipeline.status === 'awaiting_gate' ? `待确认：${pipeline.gate?.name}` : pipeline.status === 'running' ? `执行中：${stageNames[pipeline.stage] || ''}` : pipeline.status === 'failed' ? '失败' : pipeline.status === 'paused' ? '已暂停' : pipeline.status === 'tier_done' ? '本档完成' : pipeline.status}
            </span>
          )}
          <button onClick={toggleTheme} className="p-2 rounded-lg text-mute hover:text-accent hover:bg-panel2" title={theme === 'dark' ? '切换浅色' : '切换深色'}>
            {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
          </button>
          <button onClick={() => setShowSettings(true)} className="p-2 rounded-lg text-mute hover:text-accent hover:bg-panel2" title="设置"><Settings size={16} /></button>
        </header>

        <div className="flex-1 flex min-h-0">
          <main className="flex-1 min-w-0">
            <ChatView history={history} live={live} chips={chips} busy={busy}
              pipeline={pipeline} onSend={send} onChip={chipClick} onConfirmGate={confirmGate} />
          </main>
          {panelOpen && (
            <>
              {/* 拖动分隔条：调整对话栏与流水线栏比例，双击复位 */}
              <div onPointerDown={onSplitterDown}
                onDoubleClick={() => { setPanelW(PANEL_W_DEFAULT); localStorage.setItem('va-panel-w', String(PANEL_W_DEFAULT)) }}
                className="w-[5px] shrink-0 cursor-col-resize relative group hidden md:block"
                title="拖动调整左右分区比例，双击复位">
                <div className="absolute inset-y-0 left-[2px] w-px bg-edge group-hover:bg-accent group-active:bg-accent transition-colors" />
                <div className="absolute top-1/2 -translate-y-1/2 left-0 w-[5px] h-8 rounded-full bg-mute/0 group-hover:bg-accent/40 transition-colors" />
              </div>
              <aside style={{ width: panelW }} className="shrink-0 hidden md:block">
                {project ? (
                  <ArtifactPanel project={project} pipeline={pipeline} stageNames={stageNames}
                    artifacts={artifacts} activeId={activeArtifact} setActiveId={setActiveArtifact}
                    onClose={() => setPanelOpen(false)} onSend={send} onConfirmGate={confirmGate}
                    onRunStage={runStage} onStop={stopProject} />
                ) : (
                  <EmptyPipelinePanel onClose={() => setPanelOpen(false)} />
                )}
              </aside>
            </>
          )}
        </div>
      </div>

      {showSettings && <SettingsModal onClose={() => setShowSettings(false)} />}
    </div>
  )
}
