// 浏览器真机验证:程序跑通后追问功能保留
// 流程:载入除零示例 → 运行诊断(失败) → 一键应用到编辑器并重新运行 → 程序跑通
//       → 断言追问区仍在 → 提出替代方案(真跑)→ 追问对比 → 截图
import { spawn } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const CDP_PORT = 9335
const APP_URL = 'http://127.0.0.1:5173'
const TMP = path.dirname(fileURLToPath(import.meta.url))
const SHOTS = `${TMP}/shots`

mkdirSync(SHOTS, { recursive: true })
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const report = []
const say = (s) => {
  console.log(s)
  report.push(s)
}

const env = { ...process.env }
delete env.HTTP_PROXY
delete env.http_proxy
delete env.HTTPS_PROXY
delete env.https_proxy

const chrome = spawn(
  CHROME,
  [
    '--headless=new',
    `--remote-debugging-port=${CDP_PORT}`,
    `--user-data-dir=${TMP}/chrome-chat-profile2`,
    '--remote-allow-origins=*',
    '--no-proxy-server',
    '--no-first-run',
    '--hide-scrollbars',
    '--window-size=1500,950',
    'about:blank',
  ],
  { stdio: 'ignore', env },
)

async function getWsUrl() {
  for (let i = 0; i < 40; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)
      const list = await res.json()
      const page = list.find((t) => t.type === 'page')
      if (page) return page.webSocketDebuggerUrl
    } catch {}
    await sleep(250)
  }
  throw new Error('Chrome CDP 未就绪')
}

class CDP {
  constructor(ws) {
    this.ws = ws
    this.id = 0
    this.pending = new Map()
  }
  static async connect(url) {
    const ws = new WebSocket(url)
    await new Promise((res, rej) => {
      ws.onopen = res
      ws.onerror = () => rej(new Error('ws error'))
    })
    const c = new CDP(ws)
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data)
      if (msg.id && c.pending.has(msg.id)) {
        const { resolve, reject } = c.pending.get(msg.id)
        c.pending.delete(msg.id)
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result)
      }
    }
    return c
  }
  send(method, params = {}) {
    const id = ++this.id
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      this.ws.send(JSON.stringify({ id, method, params }))
    })
  }
  async eval(expr) {
    const r = await this.send('Runtime.evaluate', {
      expression: expr,
      returnByValue: true,
      awaitPromise: true,
    })
    if (r.exceptionDetails) {
      throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text)
    }
    return r.result.value
  }
  async shot(name) {
    const r = await this.send('Page.captureScreenshot', { format: 'png' })
    writeFileSync(`${SHOTS}/${name}.png`, Buffer.from(r.data, 'base64'))
    return `${SHOTS}/${name}.png`
  }
}

const SNAP = `(() => {
  const status = document.querySelector('.card.status .status-title')?.textContent?.trim() || ''
  const chat = document.querySelector('.chat-card')
  const sendBtn = chat ? chat.querySelector('.chat-send') : null
  const sendText = sendBtn ? sendBtn.textContent.trim() : ''
  const msgs = chat ? [...chat.querySelectorAll('.chat-msg')] : []
  const assistants = msgs.filter((m) => m.classList.contains('assistant'))
  const last = assistants[assistants.length - 1]
  const lastText = last ? (last.querySelector('.chat-text')?.textContent || '') : ''
  const chips = chat ? [...chat.querySelectorAll('.chip')].map((c) => c.textContent.trim()) : []
  return {
    status,
    chatPresent: Boolean(chat),
    diagPresent: Boolean(document.querySelector('.card.diagnosis')),
    hasApply: Boolean(document.querySelector('.card.diagnosis .apply')),
    chips,
    userCount: msgs.filter((m) => m.classList.contains('user')).length,
    assistantCount: assistants.length,
    lastTextLen: lastText.length,
    lastText: lastText.slice(0, 500),
    busy: sendText.includes('处理中'),
    okCards: chat ? chat.querySelectorAll('.alt-card.alt-ok').length : 0,
    failedBubbles: chat ? chat.querySelectorAll('.bubble-failed').length : 0,
  }
})()`

const okSteps = []
const assert = (name, pass) => okSteps.push([name, Boolean(pass)])

async function sendText(cdp, text, nextUserCount, timeout = 90000, waitCard = null) {
  await cdp.eval(`(() => {
    const ta = document.querySelector('.chat-input')
    ta.value = ${JSON.stringify(text)}
    ta.dispatchEvent(new Event('input', { bubbles: true }))
    return true
  })()`)
  await sleep(150)
  await cdp.eval(`(() => { document.querySelector('.chat-send').click(); return true })()`)
  const t0 = Date.now()
  let done = null
  while (Date.now() - t0 < timeout) {
    await sleep(450)
    const s = await cdp.eval(SNAP)
    if (s.userCount < nextUserCount) continue
    if (waitCard === 'alt' && !s.busy && s.okCards >= 1) {
      done = s
      break
    }
    if (waitCard === 'text' && !s.busy && s.lastTextLen > 15) {
      done = s
      break
    }
  }
  return done
}

async function main() {
  const cdp = await CDP.connect(await getWsUrl())
  await cdp.send('Page.enable')
  await cdp.send('Runtime.enable')
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 1500, height: 950, deviceScaleFactor: 1, mobile: false,
  })
  await cdp.send('Page.navigate', { url: APP_URL })
  for (let i = 0; i < 60; i++) {
    if (await cdp.eval(`!!document.querySelector('.btn.primary')`)) break
    await sleep(300)
  }
  await sleep(800)

  // ---- 步骤 0:载入除零示例 → 运行诊断(会失败)→ 等诊断完成
  await cdp.eval(`(() => {
    const sel = document.querySelector('.sample-select')
    const opt = [...sel.options].find((o) => o.textContent.includes('除零'))
    sel.value = opt.value
    sel.dispatchEvent(new Event('change'))
    return true
  })()`)
  await sleep(300)
  await cdp.eval(`(() => {
    [...document.querySelectorAll('button')].find((b) => b.textContent.includes('运行诊断')).click()
    return true
  })()`)

  let s = null
  const t0 = Date.now()
  while (Date.now() - t0 < 90000) {
    await sleep(500)
    s = await cdp.eval(SNAP)
    if (s.diagPresent && s.hasApply && s.chatPresent) break
  }
  say(`[0] 诊断完成(失败态):diag=${s?.diagPresent} apply=${s?.hasApply} chat=${s?.chatPresent} (${((Date.now() - t0) / 1000).toFixed(1)}s)`)
  assert('诊断完成(有应用按钮)', s?.hasApply)

  // ---- 步骤 1:一键应用 → 重新运行 → 程序跑通
  const t1 = Date.now()
  await cdp.eval(`(() => { document.querySelector('.card.diagnosis .apply').click(); return true })()`)
  let s2 = null
  while (Date.now() - t1 < 60000) {
    await sleep(400)
    s2 = await cdp.eval(SNAP)
    if (s2.status.includes('跑通') && s2.chatPresent && !s2.busy) break
  }
  say(`[1] 应用后重新运行:status="${s2?.status}" chatPresent=${s2?.chatPresent} (${((Date.now() - t1) / 1000).toFixed(1)}s)`)
  assert('程序跑通(状态显示「跑通了」)', (s2?.status || '').includes('跑通'))
  assert('跑通后追问区仍然存在 ★核心需求', Boolean(s2?.chatPresent))
  assert('跑通后是新会话(消息为空)', s2?.userCount === 0 && s2?.assistantCount === 0)
  say(`[1] 跑通场景快捷示例: ${JSON.stringify(s2?.chips)}`)
  assert('快捷示例切换为跑通场景话术', (s2?.chips || []).some((c) => c.includes('输入检查')))
  await cdp.eval(`(() => { const p = document.querySelector('.side-pane'); if (p) p.scrollTop = p.scrollHeight; return true })()`)
  await sleep(300)
  await cdp.shot('chat-success-01-state')

  // ---- 步骤 2:跑通场景下提出替代方案(点第 2 个 chip)→ 真实执行
  const chipText = s2?.chips?.[1] || ''
  await cdp.eval(`(() => { const chips = document.querySelectorAll('.chat-card .chip'); chips[1].click(); return true })()`)
  await sleep(150)
  const t2 = Date.now()
  const r2 = await sendText(cdp, chipText, 1, 90000, 'alt')
  say(`[2] 跑通后替代方案 "${chipText}": okCards=${r2?.okCards} (${((Date.now() - t2) / 1000).toFixed(1)}s)`)
  assert('跑通场景替代方案:真跑出结果卡', (r2?.okCards || 0) >= 1)
  await sleep(300)
  await cdp.eval(`(() => { const l = document.querySelector('.chat-list'); if (l) l.scrollTop = l.scrollHeight; const p = document.querySelector('.side-pane'); if (p) p.scrollTop = p.scrollHeight; return true })()`)
  await sleep(300)
  await cdp.shot('chat-success-02-alt')

  // ---- 步骤 3:追问「和最开始那版比改了什么」(依赖上一轮诊断背景)
  const q3 = '现在这版代码和最开始的版本相比,改了什么?'
  const t3 = Date.now()
  const r3 = await sendText(cdp, q3, 2, 60000, 'text')
  say(`[3] 追问对比 (${((Date.now() - t3) / 1000).toFixed(1)}s): ${(r3?.lastText || '').slice(0, 150)}`)
  assert('追问有完整回答', (r3?.lastTextLen || 0) > 15)
  const low3 = (r3?.lastText || '').toLowerCase()
  assert('回答引用了改动细节(len/count/0)', low3.includes('len') || low3.includes('count') || low3.includes('0'))

  const fin = await cdp.eval(SNAP)
  say(`[final] 状态="${fin.status}" 追问区=${fin.chatPresent} 消息=${fin.userCount}问${fin.assistantCount}答 失败气泡=${fin.failedBubbles}`)
  assert('无失败气泡', fin.failedBubbles === 0)
  await cdp.eval(`(() => { const p = document.querySelector('.side-pane'); if (p) p.scrollTop = p.scrollHeight; return true })()`)
  await sleep(300)
  await cdp.shot('chat-success-03-overview')

  say('\n================ 断言汇总 ================')
  let allOk = true
  for (const [name, pass] of okSteps) {
    say(`  [${pass ? 'ok' : 'FAIL'}] ${name}`)
    if (!pass) allOk = false
  }
  say('总判定: ' + (allOk ? 'PASS' : 'FAIL'))
  writeFileSync(`${TMP}/ui-chat-success-report.txt`, report.join('\n'))
  chrome.kill()
  process.exit(allOk ? 0 : 1)
}

main().catch((e) => {
  say('[ERROR] ' + (e && e.stack ? e.stack : e))
  writeFileSync(`${TMP}/ui-chat-success-report.txt`, report.join('\n'))
  chrome.kill()
  process.exit(1)
})
