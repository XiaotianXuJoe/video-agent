// API 客户端：REST + SSE 流式对话
export interface Provider { id: string; name: string; type: string; base_url: string; api_key: string; model: string; has_key?: boolean }
export interface Settings {
  providers: Provider[]
  stage_models: Record<string, string | null>
  voice: { voice_id: string; speed: number; model: string; minimax_group_id: string; minimax_api_key: string; minimax_base_url: string; has_minimax_key?: boolean }
  feishu: { app_id: string; app_secret: string; has_secret?: boolean }
}
export interface Session { id: string; title: string; project_id: string | null; updated: number; created: number }
export interface Message { id: string; role: string; content: string; kind: string; payload: any; created: number }
export interface Project { id: string; title: string; tier: string; state: string; stage: number }
export interface PipelineState { stage: number; status: string; gate: { stage: number; name: string } | null; tier: string | null; error?: string; selected_direction?: number | null }
export interface Artifact { id: string; logical_name: string; type: string; title: string; path: string; version: number; current: boolean; source: string; created: number; extra: any }
export interface Chip { label: string; action: string; tier?: string; stage?: number; artifact?: string; hint?: string; url?: string }
export interface Reference {
  id: string; title: string; source_url: string; ingested_via: string
  summary: string; key_points: string[]; usable_facts: string[]; suggested_angles: string[]
  raw_chars: number; created: number; provider?: string
}
export interface TestResult { ok: boolean; latency_ms?: number; model_echo?: string; error?: { kind: string; detail: string } }
export interface Annotation {
  id?: string; type: 'frame' | 'segment'; video_version: string
  scene_id?: number | null; frame_no?: number; t_start: number; t_end: number
  shapes: any[]; snapshot?: string; comment: string
}

const J = (r: Response) => { if (!r.ok) throw new Error(`${r.status}`); return r.json() }

export const api = {
  settings: () => fetch('/api/settings').then(J) as Promise<Settings>,
  putSettings: (s: Settings) => fetch('/api/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(s) }).then(J),
  sessions: () => fetch('/api/sessions').then(J) as Promise<Session[]>,
  newSession: () => fetch('/api/sessions', { method: 'POST' }).then(J) as Promise<Session>,
  messages: (sid: string) => fetch(`/api/sessions/${sid}/messages`).then(J) as Promise<Message[]>,
  delSession: (sid: string) => fetch(`/api/sessions/${sid}`, { method: 'DELETE' }).then(J),
  renameSession: (sid: string, title: string) => fetch(`/api/sessions/${sid}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title }) }).then(J) as Promise<Session>,
  testProvider: (p: Partial<Provider>) => fetch('/api/settings/test_provider', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider: p }) }).then(J) as Promise<TestResult>,
  testMinimax: (v: { api_key?: string; base_url?: string; model?: string; voice_id?: string }) => fetch('/api/settings/test_minimax', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(v) }).then(J) as Promise<TestResult>,
  ingest: (url: string) => fetch('/api/ingest', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) }).then(async r => { const d = await r.json(); if (!r.ok) throw Object.assign(new Error(d.detail?.detail || d.detail || '摄取失败'), { kind: d.detail?.kind }); return d }) as Promise<Reference>,
  references: () => fetch('/api/references').then(J) as Promise<Reference[]>,
  projectRefs: (pid: string) => fetch(`/api/projects/${pid}/references`).then(J) as Promise<Reference[]>,
  runStage: (pid: string, stage: number) => fetch(`/api/projects/${pid}/run_stage`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ stage }) }).then(async r => { if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || `HTTP ${r.status}`) } return r.json() }),
  stopProject: (pid: string) => fetch(`/api/projects/${pid}/stop`, { method: 'POST' }).then(J),
  attachRef: (pid: string, refId: string) => fetch(`/api/projects/${pid}/attach_ref`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ref_id: refId }) }).then(J),
  project: (pid: string) => fetch(`/api/projects/${pid}`).then(J),
  artifacts: (pid: string) => fetch(`/api/projects/${pid}/artifacts`).then(J) as Promise<Artifact[]>,
  annotations: (pid: string) => fetch(`/api/projects/${pid}/annotations`).then(J) as Promise<Annotation[]>,
  saveAnnotation: (pid: string, a: Annotation) => fetch(`/api/projects/${pid}/annotations`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(a) }).then(J),
  fileUrl: (pid: string, path: string) => `/api/projects/${pid}/file?path=${encodeURIComponent(path)}`,
  frameUrl: (pid: string, video: string, t: number) => `/api/projects/${pid}/frame?video=${encodeURIComponent(video)}&t=${t}`,
}

export interface StreamHandlers {
  onToken?: (t: string) => void
  onStage?: (d: any) => void
  onArtifact?: (a: Artifact) => void
  onGate?: (g: any) => void
  onProject?: (d: any) => void
  onSuggestions?: (chips: Chip[]) => void
  onFrontendAction?: (a: any) => void
  onSessionRenamed?: (d: { session_id: string; title: string }) => void
  onReference?: (d: { reference: Reference; attached: boolean }) => void
  onError?: (e: any) => void
  onDone?: () => void
}

export async function streamChat(sessionId: string, message: string, attachments: Annotation[] | null, h: StreamHandlers) {
  const resp = await fetch('/api/chat/stream', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, message, attachments }),
  })
  if (!resp.ok || !resp.body) { h.onError?.({ message: `HTTP ${resp.status}` }); return }
  const reader = resp.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    const parts = buf.split('\n\n')
    buf = parts.pop() || ''
    for (const part of parts) {
      const evMatch = part.match(/^event: (.+)$/m)
      const dataMatch = part.match(/^data: ([\s\S]+)$/m)
      if (!evMatch || !dataMatch) continue
      const ev = evMatch[1].trim()
      let data: any
      try { data = JSON.parse(dataMatch[1]) } catch { continue }
      if (ev === 'token') h.onToken?.(data.text)
      else if (ev === 'stage_event') h.onStage?.(data)
      else if (ev === 'artifact') h.onArtifact?.(data)
      else if (ev === 'gate') h.onGate?.(data)
      else if (ev === 'project') h.onProject?.(data)
      else if (ev === 'suggestions') h.onSuggestions?.(data.chips)
      else if (ev === 'frontend_action') h.onFrontendAction?.(data)
      else if (ev === 'session_renamed') h.onSessionRenamed?.(data)
      else if (ev === 'reference') h.onReference?.(data)
      else if (ev === 'error') h.onError?.(data)
      else if (ev === 'done') h.onDone?.()
    }
  }
  h.onDone?.()
}
