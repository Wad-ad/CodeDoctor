const JSON_HEADERS = { 'Content-Type': 'application/json' }

async function postJSON(url, payload) {
  const res = await fetch(url, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify(payload),
  })
  if (!res.ok) {
    throw new Error(`请求失败(HTTP ${res.status})`)
  }
  return res.json()
}

export function runCode(code) {
  return postJSON('/api/run', { code })
}

export function diagnose(payload) {
  return postJSON('/api/diagnose', payload)
}

/**
 * 通用 SSE 客户端:POST + 逐帧解析,事件经 onEvent 实时回调。
 * 帧格式:`data: {json}\n\n`
 */
async function postSSE(url, payload, onEvent) {
  const res = await fetch(url, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify(payload),
  })
  if (!res.ok) {
    throw new Error(`请求失败(HTTP ${res.status})`)
  }
  if (!res.body) {
    throw new Error('当前浏览器不支持流式响应')
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const frame = buf.slice(0, idx)
      buf = buf.slice(idx + 2)
      const line = frame.trim()
      if (!line.startsWith('data: ')) continue
      let evt
      try {
        evt = JSON.parse(line.slice(6))
      } catch {
        continue
      }
      onEvent(evt)
    }
  }
}

/**
 * 流式诊断:事件 {type: 'status'|'field'|'delta'|'item'|'result'|'error'|'done', ...}
 */
export function diagnoseStream(payload, onEvent) {
  return postSSE('/api/diagnose/stream', payload, onEvent)
}

/**
 * 多轮追问:事件 {type: 'session'|'status'|'delta'|'result'|'error'|'done', ...}
 * result.data.kind: 'text' 普通回答 / 'alt_success' 替代方案验证通过 / 'alt_fail' 方案走不通
 */
export function chatStream(payload, onEvent) {
  return postSSE('/api/chat/stream', payload, onEvent)
}

export async function fetchHealth() {
  const res = await fetch('/api/health')
  if (!res.ok) {
    throw new Error('后端未启动')
  }
  return res.json()
}

export async function fetchKbDocs() {
  const res = await fetch('/api/kb/docs')
  if (!res.ok) {
    throw new Error(`获取案例文档列表失败(HTTP ${res.status})`)
  }
  return res.json()
}

export async function uploadKbDoc(file) {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch('/api/kb/upload', { method: 'POST', body: form })
  if (!res.ok) {
    throw new Error(`上传失败(HTTP ${res.status})`)
  }
  return res.json()
}

export async function deleteKbDoc(docId) {
  const res = await fetch(`/api/kb/docs/${encodeURIComponent(docId)}`, { method: 'DELETE' })
  if (!res.ok) {
    throw new Error(`删除失败(HTTP ${res.status})`)
  }
  return res.json()
}
