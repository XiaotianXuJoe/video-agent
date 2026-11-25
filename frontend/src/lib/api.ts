// API 客户端：REST + SSE 流式对话
export interface Provider { id: string; name: string; type: string; base_url: string; api_key: string; model: string; has_key?: boolean }
export interface ContentRules {
  huawei_od: boolean; no_suspense_opening: boolean; no_repeat_example: boolean
  complexity_ending: boolean; integer_time: boolean
}
export interface Settings {
  providers: Provider[]
  stage_models: Record<string, string | null>
  voice: { voice_id: string; speed: number; model: string; minimax_group_id: string; minimax_api_key: string; minimax_base_url: string; has_minimax_key?: boolean }
  feishu: { app_id: string; app_secret: string; has_secret?: boolean }
  auth: { token: string; has_token?: boolean }
  security: { allow_generated_code_exec: boolean }
  content_rules: ContentRules
  pipeline: { tts_chars_per_second: number }
  bgm: { enabled: boolean; volume: number; path: string }
}
export interface Session { id: string; title: string; project_id: string | null; updated: number; created: number }
export interface Message { id: string; role: string; content: string; kind: string; payload: any; created: number }
export interface Project { id: string; title: string; tier: string; state: string; stage: number }
export interface PipelineState { stage: number; status: string; gate: { stage: number; name: string } | null; tier: string | null; error?: string; selected_direction?: number | null }
/** 流式进度（仅前端内存态）：来源 stage_event(status=progress) */
export interface StageProgress { detail?: string; percent?: number; frames?: number; total_frames?: number }
export interface RawResponseFile { name: string; size: number; mtime: number }
export interface RawResponseList { latest: RawResponseFile | null; files: RawResponseFile[] }
/** 流水线事件（D1 落表回放） */
export interface ProjectEvent { seq: number; type: string; payload: any; ts: number }
export interface ProjectEventPage { events: ProjectEvent[]; latest: number }
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
  shapes: any[]; comment: string
  /** 新格式：快照文件相对路径（annotations/ann_xxx.jpg） */
  snapshot_path?: string
  /** 兼容字段：旧数据内联的 base64 data URL */
  snapshot?: string
}

/** 标注快照展示地址：新格式走文件端点，旧数据（base64）直接内联（C-UX-09 向后兼容）。 */
export function annSnapshotUrl(pid: string, a: Annotation): string {
  if (a.snapshot_path) return api.fileUrl(pid, a.snapshot_path)
  return a.snapshot || ''
}

// ---------- 鉴权（C-SEC-01） ----------

const TOKEN_KEY = 'va_token'

export const getToken = () => localStorage.getItem(TOKEN_KEY) || ''
export const setToken = (t: string) => { if (t) localStorage.setItem(TOKEN_KEY, t); else localStorage.removeItem(TOKEN_KEY) }

/** 任何 API 返回 401 时抛出；App 顶层据此渲染登录覆盖层 */
export class AuthRequiredError extends Error {
  kind: string
  constructor(kind = 'auth_required') {
    super('AUTH_REQUIRED')
    this.name = 'AuthRequiredError'
    this.kind = kind
  }
}

function withAuth(init: RequestInit = {}): RequestInit {
  const headers: Record<string, string> = { ...(init.headers as Record<string, string> || {}) }
  const t = getToken()
  if (t) headers['Authorization'] = `Bearer ${t}`
  // cookie（va_token）同源自动携带：img/video/EventSource 等无法带请求头的场景靠它
  return { ...init, headers, credentials: 'same-origin' }
}

/** App 顶层注册：任何请求 401 都会通知这里，统一渲染登录层 */
let authListener: ((kind: string) => void) | null = null
export function onAuthRequired(fn: ((kind: string) => void) | null) { authListener = fn }

async function rawFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const resp = await fetch(input, withAuth(init))
  if (resp.status === 401) {
    let kind = 'auth_required'
    try {
      const d = await resp.clone().json()
      kind = d?.error?.kind || d?.detail?.kind || kind
    } catch { /* 非 JSON 401 也按鉴权失败处理 */ }
    authListener?.(kind)
    throw new AuthRequiredError(kind)
  }
  return resp
}

const J = (r: Response) => { if (!r.ok) throw new Error(`${r.status}`); return r.json() }
const A = (input: string, init?: RequestInit) => rawFetch(input, init).then(J)

const JSON_HEADERS = { 'Content-Type': 'application/json' }

/** 登录：密码即服务端 auth.token。成功时后端同时下发 HttpOnly cookie，前端存一份供请求头使用。 */
export async function login(password: string): Promise<void> {
  const r = await fetch('/api/auth/login', {
    method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ password }),
    credentials: 'same-origin',
  })
  if (!r.ok) {
    let kind = 'auth_failed'
    let detail = ''
    try {
      const d = await r.json()
      kind = d?.error?.kind || d?.detail?.kind || kind
      detail = d?.error?.detail || d?.detail?.detail || ''
    } catch { /* ignore */ }
    throw Object.assign(new Error(detail || kind), { kind })
  }
  setToken(password)
}

/** 登出：清后端 cookie + 本地 token。 */
export async function logout(): Promise<void> {
  try { await rawFetch('/api/auth/logout', { method: 'POST' }) } catch { /* token 已失效也照清本地 */ }
  setToken('')
}

export const api = {
  settings: () => A('/api/settings') as Promise<Settings>,
  putSettings: (s: Settings) => A('/api/settings', { method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify(s) }),
  // 测试连接只接受「已保存配置」：未保存的供应商 / 音色请先保存再测（C-SEC-02）
  testProvider: (id: string) => A('/api/settings/test_provider', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ id }) }) as Promise<TestResult>,
  testMinimax: () => A('/api/settings/test_minimax', { method: 'POST' }) as Promise<TestResult>,
  /** 上传自定义 BGM：存到服务端 assets 目录，返回文件名（不自动改设置） */
  uploadBgm: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return A('/api/settings/bgm', { method: 'POST', body: fd }) as Promise<{ path: string; bytes: number }>
  },
  sessions: () => A('/api/sessions') as Promise<Session[]>,
  newSession: () => A('/api/sessions', { method: 'POST' }) as Promise<Session>,
  messages: (sid: string) => A(`/api/sessions/${sid}/messages`) as Promise<Message[]>,
  delSession: (sid: string) => A(`/api/sessions/${sid}`, { method: 'DELETE' }),
  renameSession: (sid: string, title: string) => A(`/api/sessions/${sid}`, { method: 'PATCH', headers: JSON_HEADERS, body: JSON.stringify({ title }) }) as Promise<Session>,
  ingest: (url: string) => rawFetch('/api/ingest', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ url }) }).then(async r => { const d = await r.json(); if (!r.ok) throw Object.assign(new Error(d.detail?.detail || d.detail || '摄取失败'), { kind: d.detail?.kind }); return d }) as Promise<Reference>,
  references: () => A('/api/references') as Promise<Reference[]>,
  projectRefs: (pid: string) => A(`/api/projects/${pid}/references`) as Promise<Reference[]>,
  runStage: (pid: string, stage: number) => rawFetch(`/api/projects/${pid}/run_stage`, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ stage }) }).then(async r => { if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || `HTTP ${r.status}`) } return r.json() }),
  stopProject: (pid: string) => A(`/api/projects/${pid}/stop`, { method: 'POST' }),
  attachRef: (pid: string, refId: string) => A(`/api/projects/${pid}/attach_ref`, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ ref_id: refId }) }),
  project: (pid: string) => A(`/api/projects/${pid}`),
  artifacts: (pid: string) => A(`/api/projects/${pid}/artifacts`) as Promise<Artifact[]>,
  rawResponses: (pid: string) => A(`/api/projects/${pid}/raw_responses`) as Promise<RawResponseList>,
  annotations: (pid: string) => A(`/api/projects/${pid}/annotations`) as Promise<Annotation[]>,
  saveAnnotation: (pid: string, a: Annotation) => A(`/api/projects/${pid}/annotations`, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(a) }),
  /** 流水线事件增量回放（D1）：断线/刷新后按 seq 游标拉取，替代盲刷轮询 */
  projectEvents: (pid: string, after = 0) => A(`/api/projects/${pid}/events?after=${after}`) as Promise<ProjectEventPage>,
  logout,
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
  const resp = await rawFetch('/api/chat/stream', {
    method: 'POST', headers: JSON_HEADERS,
    body: JSON.stringify({ session_id: sessionId, message, attachments }),
  })
  if (!resp.ok || !resp.body) { h.onError?.({ message: `HTTP ${resp.status}` }); return }
  const reader = resp.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  let doneCalled = false
  const fireDone = () => { if (!doneCalled) { doneCalled = true; h.onDone?.() } }
  try {
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
        else if (ev === 'ping') { /* D1 心跳保活：忽略 */ }
        else if (ev === 'done') fireDone()
      }
    }
  } catch (e) {
    // 读流中断（C-REL-02/03）：先按 done 收口（busy 复位、消息落历史），再把错误抛给调用方展示
    fireDone()
    throw e
  }
  fireDone()
}
