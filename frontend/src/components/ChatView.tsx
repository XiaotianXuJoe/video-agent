import { useEffect, useRef, useState } from 'react'
import { SendHorizonal, Loader2, ShieldCheck } from 'lucide-react'
import { Message, Chip, PipelineState, Annotation } from '../lib/api'
import { LiveMsg } from '../App'

const ACTION_LABELS: Record<string, string> = {
  start_tier: '开始制作', confirm_gate: '确认通过', upgrade_tier: '继续到下一档',
  redo_stage: '重新执行阶段', retry_stage: '重试', resume_pipeline: '继续执行后续阶段', finish: '审片通过',
  ingest_url: '摄取并整理链接',
}
function friendlyAction(content: string): string | null {
  if (!content.startsWith('__action:')) return null
  try {
    const a = JSON.parse(content.slice(9))
    const tierName: Record<string, string> = { concept: '创意档', storyboard: '分镜档', video: '成片档' }
    if (a.action === 'start_tier') return `开始制作（${tierName[a.tier] || a.tier}）`
    if (a.action === 'upgrade_tier') return `继续到${tierName[a.tier] || a.tier}`
    if (a.action === 'confirm_gate') return a.selected_direction != null ? `确认创意（方向 ${a.selected_direction + 1}）` : '确认通过'
    return ACTION_LABELS[a.action] || a.action
  } catch { return '操作' }
}

function renderText(t: string) {
  // markdown-lite：**粗体**、`代码`、换行
  return t.split('\n').map((line, i) => {
    const parts = line.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((p, j) => {
      if (p.startsWith('**') && p.endsWith('**')) return <strong key={j} className="text-ink font-semibold">{p.slice(2, -2)}</strong>
      if (p.startsWith('`') && p.endsWith('`')) return <code key={j} className="mono text-accent text-[0.85em] bg-panel2 px-1 rounded">{p.slice(1, -1)}</code>
      return <span key={j}>{p}</span>
    })
    return <div key={i} className="min-h-[1em]">{parts}</div>
  })
}

export default function ChatView({ history, live, chips, busy, pipeline, onSend, onChip, onConfirmGate }: {
  history: Message[]; live: LiveMsg | null; chips: Chip[]; busy: boolean
  pipeline: PipelineState | null
  onSend: (t: string) => void; onChip: (c: Chip) => void; onConfirmGate: (sel?: number) => void
}) {
  const [input, setInput] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [history, live])

  const submit = () => {
    const t = input.trim()
    if (!t || busy) return
    setInput(''); onSend(t)
  }

  return (
    <div className="h-full flex flex-col">
      <div className="flex-1 overflow-y-auto px-4 md:px-8 py-6">
        <div className="max-w-[760px] mx-auto space-y-5">
          {history.length === 0 && !live && (
            <div className="text-center pt-24 fade-in">
              <div className="text-2xl font-semibold mb-2">把题解做成讲解视频</div>
              <div className="text-mute text-sm leading-7">
                发给我题目和正确代码，或者一个科普主题。<br />
                我会按「创意 → 分镜 → 视觉 → 配音 → 渲染 → 验收」流程制作，每一步你都可以确认或修改。
              </div>
            </div>
          )}
          {history.map(m => {
            const actionLabel = m.role === 'user' ? friendlyAction(m.content) : null
            return (
              <div key={m.id} className={`fade-in flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                <div className={m.role === 'user'
                  ? 'max-w-[85%] bg-accent/12 border border-accent/25 rounded-2xl rounded-br-md px-4 py-2.5 text-ink'
                  : 'max-w-[92%] text-ink/90'}>
                  {m.kind === 'annotation' && <div className="text-xs text-purplex mb-1">📎 附带视频标注</div>}
                  {actionLabel
                    ? <span className="inline-flex items-center gap-1.5 text-xs text-accent"><span className="w-1.5 h-1.5 rounded-full bg-accent inline-block" />{actionLabel}</span>
                    : renderText(m.content)}
                </div>
              </div>
            )
          })}
          {live && (
            <div className="fade-in">
              <div className="max-w-[92%] text-ink/90">
                {renderText(live.text)}
                {live.streaming && <span className="inline-block w-2 h-4 bg-accent/70 align-middle ml-0.5 animate-pulse" />}
              </div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      </div>

      <div className="shrink-0 px-4 md:px-8 pb-4 pt-1">
        <div className="max-w-[760px] mx-auto">
          {pipeline?.status === 'awaiting_gate' && (
            <div className="mb-2 flex items-center gap-2 text-amberx text-xs bg-amberx/10 border border-amberx/25 rounded-lg px-3 py-2">
              <ShieldCheck size={14} />
              人工门禁：{pipeline.gate?.name}。在右侧查看产物后确认，或直接输入修改意见。
              <button onClick={() => onConfirmGate()} className="ml-auto px-2.5 py-1 rounded-md bg-amberx text-base font-medium text-onaccent hover:brightness-110">确认通过</button>
            </div>
          )}
          {chips.length > 0 && (
            <div className="flex flex-wrap gap-2 mb-2.5">
              {chips.map((c, i) => (
                <button key={i} onClick={() => onChip(c)} title={c.hint}
                  className="px-3 py-1.5 rounded-full text-xs border border-edge bg-panel2 text-mute hover:text-accent hover:border-accent/50 transition-colors">
                  {c.label}
                </button>
              ))}
            </div>
          )}
          <div className="flex items-end gap-2 bg-panel2 border border-edge rounded-2xl p-2 transition-colors focus-within:border-accent/60"
            style={{ boxShadow: 'var(--shadow)' }}>
            <textarea
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit() } }}
              placeholder="输入消息，Enter 发送 / Shift+Enter 换行"
              rows={2}
              className="flex-1 resize-none bg-transparent px-3 py-2 text-ink placeholder:text-mute/60 focus:outline-none"
            />
            <button onClick={submit} disabled={busy || !input.trim()}
              className="p-2.5 rounded-xl bg-accent text-onaccent hover:brightness-110 disabled:opacity-40 disabled:cursor-not-allowed transition-all">
              {busy ? <Loader2 size={18} className="animate-spin" /> : <SendHorizonal size={18} />}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
