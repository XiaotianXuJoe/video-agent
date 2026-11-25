import { useCallback, useEffect, useRef, useState } from 'react'
import { api, Artifact, Project, Annotation } from '../lib/api'
import { Flag, Scissors, Send, SquarePen, X } from 'lucide-react'

interface Shape { kind: 'rect'; x: number; y: number; w: number; h: number } // 视频原始像素坐标

export default function VideoViewer({ project, artifact, onSend }: {
  project: Project; artifact: Artifact
  onSend: (t: string, a?: Annotation[] | null) => void
}) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const [timeline, setTimeline] = useState<any>(null)
  const [curScene, setCurScene] = useState<number>(-1)
  const [curTime, setCurTime] = useState(0)
  const [mode, setMode] = useState<'off' | 'frame' | 'segment'>('off')
  const [shapes, setShapes] = useState<Shape[]>([])
  const [draft, setDraft] = useState<Shape | null>(null)
  const [seg, setSeg] = useState<{ a: number | null; b: number | null }>({ a: null, b: null })
  const [comment, setComment] = useState('')
  const [showComment, setShowComment] = useState(false)
  const videoSrc = api.fileUrl(project.id, artifact.path)

  useEffect(() => {
    fetch(api.fileUrl(project.id, 'timeline.json')).then(r => r.json()).then(setTimeline).catch(() => {})
  }, [project.id, artifact.id])

  const sceneAt = useCallback((t: number) => {
    if (!timeline) return -1
    for (const sc of timeline.scenes) {
      if (t >= (sc.start || 0) && t < (sc.start || 0) + (sc.duration || 0)) return sc.id
    }
    return timeline.scenes.length ? timeline.scenes[timeline.scenes.length - 1].id : -1
  }, [timeline])

  const onTime = () => {
    const v = videoRef.current; if (!v) return
    setCurTime(v.currentTime)
    const sid = sceneAt(v.currentTime)
    if (sid !== curScene) {
      setCurScene(sid)
      listRef.current?.querySelector(`[data-scene="${sid}"]`)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
    }
  }

  const seek = (t: number) => { const v = videoRef.current; if (v) { v.currentTime = t + 0.01; v.play() } }

  // ---- 帧标注：覆盖层拖框（存储为视频原始像素） ----
  const toNatural = (e: React.MouseEvent) => {
    const v = videoRef.current!, r = v.getBoundingClientRect()
    const sx = v.videoWidth / r.width, sy = v.videoHeight / r.height
    return { x: (e.clientX - r.left) * sx, y: (e.clientY - r.top) * sy }
  }
  const dragStart = useRef<{ x: number; y: number } | null>(null)
  const onDown = (e: React.MouseEvent) => { if (mode !== 'frame') return; dragStart.current = toNatural(e) }
  const onMove = (e: React.MouseEvent) => {
    if (mode !== 'frame' || !dragStart.current) return
    const p = toNatural(e), s = dragStart.current
    setDraft({ kind: 'rect', x: Math.min(s.x, p.x), y: Math.min(s.y, p.y), w: Math.abs(p.x - s.x), h: Math.abs(p.y - s.y) })
  }
  const onUp = () => {
    if (draft && draft.w > 8 && draft.h > 8) { setShapes(s => [...s, draft]); setShowComment(true) }
    setDraft(null); dragStart.current = null
  }

  const snapshot = (): string => {
    const v = videoRef.current!
    const c = document.createElement('canvas')
    c.width = v.videoWidth; c.height = v.videoHeight
    const ctx = c.getContext('2d')!
    ctx.drawImage(v, 0, 0)
    ctx.strokeStyle = '#5DE2E7'; ctx.lineWidth = 6
    for (const s of shapes) ctx.strokeRect(s.x, s.y, s.w, s.h)
    return c.toDataURL('image/jpeg', 0.72)
  }

  const buildAnnotation = (): Annotation => {
    const v = videoRef.current!
    if (mode === 'segment') {
      const a = seg.a ?? 0, b = seg.b ?? v.currentTime
      return { type: 'segment', video_version: artifact.id, t_start: Math.min(a, b), t_end: Math.max(a, b), shapes: [], comment }
    }
    return { type: 'frame', video_version: artifact.id, t_start: v.currentTime, t_end: v.currentTime, shapes: [...shapes], snapshot: snapshot(), comment }
  }

  const sendBack = async () => {
    const ann = buildAnnotation()
    const saved = await api.saveAnnotation(project.id, ann)
    const text = comment || (ann.type === 'frame' ? '标注位置需要修改' : '这个片段需要修改')
    onSend(text, [saved])
    setShapes([]); setComment(''); setShowComment(false); setMode('off'); setSeg({ a: null, b: null })
  }

  const fmt = (t: number) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}`

  return (
    <div className="flex flex-col h-full">
      {/* 视频区 */}
      <div ref={wrapRef} className="relative shrink-0 bg-black select-none">
        <video ref={videoRef} src={videoSrc} controls className="w-full max-h-[46vh]" onTimeUpdate={onTime} />
        {(mode === 'frame') && (
          <svg className="absolute inset-0 w-full h-full cursor-crosshair" style={{ maxHeight: '46vh' }}
            onMouseDown={onDown} onMouseMove={onMove} onMouseUp={onUp}>
            <Rects shapes={shapes} draft={draft} videoRef={videoRef} />
          </svg>
        )}
        {mode === 'off' && shapes.length > 0 && (
          <svg className="absolute inset-0 w-full h-full pointer-events-none" style={{ maxHeight: '46vh' }}>
            <Rects shapes={shapes} draft={null} videoRef={videoRef} />
          </svg>
        )}
      </div>

      {/* 时间轴 filmstrip（按镜头分段着色 + 缩略图） */}
      {timeline && (
        <div className="shrink-0 flex h-14 border-y border-edge overflow-x-auto bg-base/60">
          {timeline.scenes.map((sc: any) => {
            const total = timeline.total_duration || timeline.scenes.reduce((a: number, s: any) => a + (s.duration || 0), 0)
            const w = Math.max(64, ((sc.duration || 0) / total) * 100)
            const active = curScene === sc.id
            return (
              <button key={sc.id} onClick={() => seek(sc.start || 0)}
                className={`relative shrink-0 border-r border-edge group ${active ? 'bg-accent/15' : 'hover:bg-panel2'}`}
                style={{ width: `${w}%`, minWidth: 64 }} title={`#${sc.id} ${sc.title}`}>
                <img loading="lazy" src={api.frameUrl(project.id, artifact.path, (sc.start || 0) + 0.15)}
                  className="absolute inset-0 w-full h-full object-cover opacity-45 group-hover:opacity-65 transition-opacity" alt="" />
                <span className={`absolute left-1 top-0.5 mono text-[10px] ${active ? 'text-accent' : 'text-ink/70'}`}>#{sc.id}</span>
                <span className="absolute bottom-0.5 left-1 right-1 text-[10px] text-ink/80 truncate">{sc.title}</span>
              </button>
            )
          })}
        </div>
      )}

      {/* 标注工具条 */}
      <div className="shrink-0 flex items-center gap-2 px-3 py-2 border-b border-edge text-xs">
        <span className="mono text-mute">{fmt(curTime)}{curScene > 0 && ` · 镜头 #${curScene}`}</span>
        <div className="flex-1" />
        <button onClick={() => { const v = videoRef.current; v?.pause(); setMode(mode === 'frame' ? 'off' : 'frame'); setSeg({ a: null, b: null }) }}
          className={`flex items-center gap-1 px-2.5 py-1.5 rounded-md border ${mode === 'frame' ? 'border-accent text-accent bg-accent/10' : 'border-edge text-mute hover:text-ink'}`}>
          <SquarePen size={13} /> 标注帧
        </button>
        <button onClick={() => { setMode(mode === 'segment' ? 'off' : 'segment'); setShapes([]) }}
          className={`flex items-center gap-1 px-2.5 py-1.5 rounded-md border ${mode === 'segment' ? 'border-purplex text-purplex bg-purplex/10' : 'border-edge text-mute hover:text-ink'}`}>
          <Scissors size={13} /> 标记片段
        </button>
        {mode === 'segment' && (
          <>
            <button onClick={() => setSeg(s => ({ ...s, a: videoRef.current?.currentTime ?? 0 }))} className="px-2 py-1.5 rounded-md border border-edge text-mute hover:text-purplex">
              <Flag size={13} className="inline mr-1" />入点 {seg.a !== null && <b className="mono text-purplex">{fmt(seg.a)}</b>}
            </button>
            <button onClick={() => { setSeg(s => ({ ...s, b: videoRef.current?.currentTime ?? 0 })); setShowComment(true) }} className="px-2 py-1.5 rounded-md border border-edge text-mute hover:text-purplex">
              <Flag size={13} className="inline mr-1" />出点 {seg.b !== null && <b className="mono text-purplex">{fmt(seg.b)}</b>}
            </button>
          </>
        )}
      </div>

      {/* 标注说明 + 回传 */}
      {showComment && (
        <div className="shrink-0 px-3 py-2 border-b border-edge bg-panel2/40 fade-in">
          <div className="flex items-center gap-2">
            <input value={comment} onChange={e => setComment(e.target.value)}
              placeholder="说明要改什么，如「字幕超出安全区」「这段发音不对」"
              className="flex-1 bg-base border border-edge rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-accent/60" />
            <button onClick={sendBack} className="flex items-center gap-1 px-3 py-2 rounded-lg bg-accent text-[#0B1220] text-xs font-medium hover:brightness-110">
              <Send size={13} /> 回传对话
            </button>
            <button onClick={() => { setShapes([]); setSeg({ a: null, b: null }); setShowComment(false); setComment('') }}
              className="p-2 text-mute hover:text-ink"><X size={14} /></button>
          </div>
        </div>
      )}

      {/* 分镜稿（与视频同步高亮） */}
      <div ref={listRef} className="flex-1 overflow-y-auto min-h-0">
        {timeline ? timeline.scenes.map((sc: any) => {
          const active = curScene === sc.id
          return (
            <div key={sc.id} data-scene={sc.id} onClick={() => seek(sc.start || 0)}
              className={`px-4 py-3 border-b border-edge/60 cursor-pointer transition-colors ${active ? 'bg-accent/10 border-l-2 border-l-accent' : 'hover:bg-panel2/50 border-l-2 border-l-transparent'}`}>
              <div className="flex items-center gap-2 mb-1">
                <span className={`mono text-xs ${active ? 'text-accent' : 'text-mute'}`}>#{sc.id}</span>
                <span className={`text-sm font-medium ${active ? 'text-ink' : 'text-ink/80'}`}>{sc.title}</span>
                <span className="ml-auto mono text-[10px] text-mute">{fmt(sc.start || 0)} · {(sc.duration || 0).toFixed(1)}s</span>
              </div>
              <div className="text-xs text-mute line-clamp-2">{sc.narration}</div>
              {(sc.captions || []).slice(0, 2).map((c: string, j: number) => (
                <div key={j} className="mono text-[11px] text-greenx/80 mt-0.5">▸ {c}</div>
              ))}
            </div>
          )
        }) : <div className="p-4 text-mute text-sm">时间轴加载中…</div>}
      </div>
    </div>
  )
}

function Rects({ shapes, draft, videoRef }: { shapes: Shape[]; draft: Shape | null; videoRef: React.RefObject<HTMLVideoElement | null> }) {
  const v = videoRef.current
  if (!v || !v.videoWidth) return null
  const r = v.getBoundingClientRect()
  const kx = r.width / v.videoWidth, ky = r.height / v.videoHeight
  const draw = (s: Shape, key: string, dash?: boolean) => (
    <rect key={key} x={s.x * kx} y={s.y * ky} width={s.w * kx} height={s.h * ky}
      fill="rgba(93,226,231,0.12)" stroke="#5DE2E7" strokeWidth={2} strokeDasharray={dash ? '6 4' : undefined} />
  )
  return <>{shapes.map((s, i) => draw(s, `s${i}`))}{draft && draw(draft, 'draft', true)}</>
}
