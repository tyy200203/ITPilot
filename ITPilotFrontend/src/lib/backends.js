const DEFAULT_BACKENDS = {
  python: {
    id: 'python',
    label: 'Python',
    baseUrl: import.meta.env.VITE_PYTHON_API_URL || '/api/python',
    port: '8000'
  }
}

export function createInitialSettings() {
  const saved = readSettings()
  const auth = readAuth()
  return {
    backend: 'python',
    userId: saved.userId || 'u1001',
    conversationId: saved.conversationId || '',
    userToken: auth.userToken || '',
    adminToken: auth.adminToken || '',
    endpoints: {
      python: saved.endpoints?.python || DEFAULT_BACKENDS.python.baseUrl
    }
  }
}

export function saveSettings(settings) {
  const { userToken, adminToken, ...publicSettings } = settings
  localStorage.setItem('itpilot.frontend.settings', JSON.stringify(publicSettings))
  sessionStorage.setItem('itpilot.frontend.auth', JSON.stringify({ userToken, adminToken }))
}

export function backendMeta(type, settings) {
  const meta = DEFAULT_BACKENDS[type] || DEFAULT_BACKENDS.python
  return {
    ...meta,
    baseUrl: normalizeBaseUrl(settings.endpoints?.[meta.id] || meta.baseUrl)
  }
}

export async function requestHealth(type, settings) {
  return requestJson(backendMeta(type, settings).baseUrl, '/health')
}

export async function requestMonitor(type, settings) {
  return authenticatedRequest(type, settings, '/monitor', {}, 'admin')
}

export async function requestSkills(type, settings) {
  return authenticatedRequest(type, settings, '/skills', {}, 'admin')
}

export async function reloadSkills(type, settings) {
  return authenticatedRequest(type, settings, '/skills/reload', { method: 'POST' }, 'admin')
}

export async function requestKnowledgeStats(type, settings) {
  return authenticatedRequest(type, settings, '/knowledge/stats')
}

export async function runEvaluation(type, settings, body = null) {
  return authenticatedRequest(type, settings, '/eval/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined
  }, 'admin')
}

export async function requestSearch(type, settings, query, topK = 5) {
  const params = new URLSearchParams({ query, top_k: String(topK) })
  return authenticatedRequest(type, settings, `/search?${params}`, { method: 'POST' })
}

export async function requestChat(type, settings, message) {
  const payload = buildChatPayload(type, settings, message)
  const raw = await authenticatedRequest(type, settings, '/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
  return normalizeChatResponse(type, raw)
}

export async function archiveConversation(type, settings, userId, conversationId) {
  return authenticatedRequest(type, settings, '/conversation/archive', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      user_id: userId || 'anonymous',
      conv_id: conversationId
    })
  })
}

export async function requestToolTrace(type, settings, requestId) {
  if (!requestId) return null
  const raw = await authenticatedRequest(type, settings, `/trace/tool/${encodeURIComponent(requestId)}`)
  return normalizeToolTraceResponse(raw)
}

export async function addKnowledge(type, settings, documents) {
  return authenticatedRequest(type, settings, '/knowledge/add', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ documents })
  }, 'admin')
}

export async function uploadKnowledge(type, settings, file) {
  const form = new FormData()
  form.append('file', file)
  return authenticatedRequest(type, settings, '/knowledge/upload', {
    method: 'POST',
    body: form
  }, 'admin')
}

function buildChatPayload(type, settings, message) {
  return {
    message,
    user_id: settings.userId || 'anonymous',
    conv_id: settings.conversationId || undefined
  }
}

function normalizeChatResponse(type, raw) {
  return {
    backend: type,
    conversationId: raw.conversation_id || raw.conversationId || raw.conv_id || '',
    requestId: raw.request_id || raw.requestId || '',
    response: raw.response || '',
    intent: raw.intent || 'other',
    intentGroup: raw.intent_group || raw.intentGroup || 'other',
    agentType: raw.agent_type || raw.agentType || '',
    agentTypes: raw.agent_types || raw.agentTypes || [],
    primaryAgent: raw.primary_agent || raw.primaryAgent || '',
    supportingAgents: raw.supporting_agents || raw.supportingAgents || [],
    routingReason: raw.routing_reason || raw.routingReason || '',
    routingConfidence: Number(raw.routing_confidence ?? raw.routingConfidence ?? 0),
    entities: raw.entities || {},
    intentConfidence: Number(raw.intent_confidence ?? raw.intentConfidence ?? 0),
    intentSourceScores: raw.intent_source_scores || raw.intentSourceScores || {},
    escalated: Boolean(raw.escalated),
    latencyMs: Number(raw.latency_ms ?? raw.latencyMs ?? 0),
    knowledgeUsed: Boolean(raw.knowledge_used ?? raw.knowledgeUsed),
    verified: raw.verified,
    grounded: raw.grounded,
    raw
  }
}

function normalizeToolTraceResponse(raw) {
  const trace = raw?.trace || {}
  return {
    requestId: raw?.request_id || raw?.requestId || '',
    found: Boolean(raw?.found),
    trace: {
      ...trace,
      toolsUsed: trace.tools_used || trace.toolsUsed || [],
      toolCalls: trace.tool_calls || trace.toolCalls || []
    },
    raw
  }
}

async function requestJson(baseUrl, path, options = {}) {
  const url = `${normalizeBaseUrl(baseUrl)}${path}`
  const response = await fetch(url, options)
  const text = await response.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }
  if (!response.ok) {
    const detail = typeof data === 'string' ? data : JSON.stringify(data)
    throw new Error(`${response.status} ${response.statusText}: ${detail}`)
  }
  return data
}

function authenticatedRequest(type, settings, path, options = {}, scope = 'user') {
  const token = scope === 'admin' ? settings.adminToken : settings.userToken
  return requestJson(backendMeta(type, settings).baseUrl, path, {
    ...options,
    headers: {
      ...(options.headers || {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {})
    }
  })
}

function normalizeBaseUrl(value) {
  return String(value || '').replace(/\/+$/, '')
}

function readSettings() {
  try {
    const current = localStorage.getItem('itpilot.frontend.settings')
    const legacy = localStorage.getItem('echomind.frontend.settings')
    return JSON.parse(current || legacy || '{}')
  } catch {
    return {}
  }
}

function readAuth() {
  try {
    return JSON.parse(sessionStorage.getItem('itpilot.frontend.auth') || '{}')
  } catch {
    return {}
  }
}
