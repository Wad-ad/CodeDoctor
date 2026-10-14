// 浏览器真机验证:多轮追问交互
// 流程:运行诊断 → 出现追问区 → ①普通追问(流式) ②替代方案通过卡 ③走不通结构化卡
// 运行:node ui-chat-verify.mjs
import { spawn } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const CDP_PORT = 9334
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
    `--user-data-dir=${TMP}/chrome-chat-profile`,
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

const CHAT_SNAP = `(() => {
  const card = document.querySelector('.chat-card')
  if (!card) return { present: false }
  const msgs = [...card.querySelectorAll('.chat-msg')]
  const assistants = msgs.filter((m) => m.classList.contains('assistant'))
  const last = assistants[assistants.length - 1]
  const lastText = last ? (last.querySelector('.chat-text')?.textContent || '') : ''
  const lastStatus = last ? (last.querySelector('.chat-status')?.textContent?.trim() || '') : ''
  const sendBtn = card.querySelector('.chat-send')
  const sendText = sendBtn ? sendBtn.textContent.trim() : ''
  const input = card.querySelector('.chat-input')
  const allText = card.innerText || ''
  return {
    present: true,
    total: msgs.length,
    userCount: msgs.filter((m) => m.classList.contains('user')).length,
    assistantCount: assistants.length,
    lastTextLen: lastText.length,
    lastText: lastText.slice(0, 500),
    lastStatus,
    sendText,
    busy: sendText.includes('处理中'),
    inputValue: input ? input.value : '',
    okCards: card.querySelectorAll('.alt-card.alt-ok').length,
    failCards: card.querySelectorAll('.alt-card.alt-fail').length,
    failedBubbles: card.querySelectorAll('.bubble-failed').length,
    failSections: [...card.querySelectorAll('.alt-card.alt-fail .alt-section-title')].map((e) => e.textContent.trim()),
    failHead: card.querySelector('.alt-card.alt-fail .alt-head')?.textContent?.trim() || '',
    okHead: card.querySelector('.alt-card.alt-ok .alt-head')?.textContent?.trim() || '',
    okOutput: card.querySelector('.alt-card.alt-ok .verify-note')?.textContent?.trim() || '',
    mentionsScipy: allText.toLowerCase().includes('scipy'),
  }
})()`

async function scrollAndShot(cdp, name) {
  await cdp.eval(`(() => {
    const p = document.querySelector('.side-pane')
    if (p) p.scrollTop = p.scrollHeight
    const l = document.querySelector('.chat-list')
    if (l) l.scrollTop = l.scrollHeight
    return true
  })()`)
  await sleep(350)
  await cdp.shot(name)
}

async function typeMessage(cdp, text, nextUserCount) {
  await cdp.eval(`(() => {
    const ta = document.querySelector('.chat-input')
    ta.focus()
    ta.value = ${JSON.stringify(text)}
    ta.dispatchEvent(new Event('input', { bubbles: true }))
    return ta.value === ${JSON.stringify(text)}
  })()`)
  await sleep(150)
  const clicked = await cdp.eval(`(() => {
    const btn = document.querySelector('.chat-send')
    if (!btn) return false
    btn.click()
    return true
  })()`)
  if (!clicked) throw new Error('找不到发送按钮')
  // 确认用户消息已入列
  const t0 = Date.now()
  while (Date.now() - t0 < 6000) {
    const s = await cdp.eval(CHAT_SNAP)
    if (s.userCount >= nextUserCount) return s
    await sleep(200)
  }
  throw new Error(`消息未发出(期望 userCount>=${nextUserCount})`)
}

const okSteps = []
function assert(name, pass) {
  okSteps.push([name, Boolean(pass)])
}

async function main() {
  const cdp = await CDP.connect(await getWsUrl())
  await cdp.send('Page.enable')
  await cdp.send('Runtime.enable')
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 1500,
    height: 950,
    deviceScaleFactor: 1,
    mobile: false,
  })

  await cdp.send('Page.navigate', { url: APP_URL })
  for (let i = 0; i < 60; i++) {
    const ready = await cdp.eval(`!!document.querySelector('.btn.primary')`)
    if (ready) break
    await sleep(300)
  }
  await sleep(800)

  const badge = await cdp.eval(`document.querySelector('.ai-badge')?.textContent?.trim() || ''`)
  say(`[env] AI 状态徽标: ${badge}`)

  // ---- 步骤 0:运行诊断,等诊断完成 + 追问区出现
  await cdp.eval(`(() => {
    const sel = document.querySelector('.sample-select')
    const opt = [...sel.options].find((o) => o.textContent.includes('除零'))
    sel.value = opt.value
    sel.dispatchEvent(new Event('change'))
    return true
  })()`)
  await sleep(300)
  const t0 = Date.now()
  await cdp.eval(`(() => {
    const btn = [...document.querySelectorAll('button')].find((b) => b.textContent.includes('运行诊断'))
    btn.click()
    return true
  })()`)

  let chatSnap = null
  while (Date.now() - t0 < 90000) {
    await sleep(500)
    const s = await cdp.eval(CHAT_SNAP)
    if (s.present) {
      chatSnap = s
      break
    }
  }
  say(`[0] 诊断完成、追问区出现: ${chatSnap ? 'yes' : 'NO'} (${((Date.now() - t0) / 1000).toFixed(1)}s)`)
  assert('诊断后出现追问区', chatSnap)
  const diagVerified = await cdp.eval(
    `!!document.querySelector('.card.diagnosis .tag-verified')`,
  )
  assert('诊断卡带「已通过真实验证」', diagVerified)
  if (!chatSnap) throw new Error('追问区未出现,终止')

  const chipText = await cdp.eval(`document.querySelector('.chat-card .chip')?.textContent?.trim() || ''`)
  say(`[0] 示例问题 chip: "${chipText}"`)
  assert('存在示例问题 chip', chipText.length > 0)

  // ---- 步骤 1:普通追问(点 chip → 发送),验证流式文字
  await cdp.eval(`(() => { document.querySelector('.chat-card .chip').click(); return true })()`)
  await sleep(150)
  const chipFilled = await cdp.eval(`document.querySelector('.chat-card .chat-input')?.value || ''`)
  assert('点 chip 自动填入输入框', chipFilled === chipText && chipFilled.length > 0)

  const t1 = Date.now()
  const s1 = await typeMessage(cdp, chipFilled, 1)
  say(`[1] 已发送问题: "${chipFilled}"`)
  let firstTextAt = null
  let lenProgression = []
  let done1 = null
  while (Date.now() - t1 < 60000) {
    await sleep(350)
    const s = await cdp.eval(CHAT_SNAP)
    lenProgression.push(s.lastTextLen)
    if (firstTextAt === null && s.lastTextLen > 0) firstTextAt = (Date.now() - t1) / 1000
    if (s.lastStatus) say(`[1] +${((Date.now() - t1) / 1000).toFixed(1)}s 状态: ${s.lastStatus}`)
    if (!s.busy && s.lastTextLen > 10) {
      done1 = s
      break
    }
    if (Date.now() - t1 > 55000) break
  }
  const grew1 = lenProgression.filter((n, i) => i > 0 && n > lenProgression[i - 1]).length
  say(`[1] 首字 ${firstTextAt === null ? 'N/A' : firstTextAt + 's'};回答长度 ${done1?.lastTextLen ?? 'N/A'};长度递增次数 ${grew1}`)
  assert('普通追问:流式文字到达', firstTextAt !== null)
  assert('普通追问:回答完整(>15字)', (done1?.lastTextLen || 0) > 15)
  assert('普通追问:过程中文字渐进增长(>=2次)', grew1 >= 2)
  say(`[1] 回答节选: ${(done1?.lastText || '').slice(0, 120).replace(/\\n/g, ' ')}`)
  await scrollAndShot(cdp, 'chat-01-question')

  // ---- 步骤 2:替代方案 - 真实跑通
  const msg2 = '为什么不直接用 average = sum(scores) / len(scores) 来算?'
  const t2 = Date.now()
  await typeMessage(cdp, msg2, 2)
  say(`[2] 已发送: "${msg2}"`)
  let done2 = null
  const statuses2 = new Set()
  while (Date.now() - t2 < 90000) {
    await sleep(450)
    const s = await cdp.eval(CHAT_SNAP)
    if (s.lastStatus) statuses2.add(s.lastStatus)
    if (!s.busy && s.okCards >= 1) {
      done2 = s
      break
    }
    if (Date.now() - t2 > 85000) break
  }
  for (const st of statuses2) say(`[2] 状态轨迹: ${st}`)
  say(`[2] 替代方案卡: head="${done2?.okHead}" 输出="${done2?.okOutput}"`)
  assert('替代方案:出现「验证通过」卡', Boolean(done2))
  assert('替代方案:卡片含真实运行输出', (done2?.okOutput || '').includes('88.5'))
  await scrollAndShot(cdp, 'chat-02-alt-success')

  // ---- 步骤 3:替代方案 - 走不通(scipy 未安装)
  const msg3 = '能不能用 scipy 的 mean 函数来算平均分?'
  const t3 = Date.now()
  await typeMessage(cdp, msg3, 3)
  say(`[3] 已发送: "${msg3}"`)
  let done3 = null
  const statuses3 = new Set()
  while (Date.now() - t3 < 120000) {
    await sleep(500)
    const s = await cdp.eval(CHAT_SNAP)
    if (s.lastStatus) statuses3.add(s.lastStatus)
    if (!s.busy && s.failCards >= 1) {
      done3 = s
      break
    }
    if (Date.now() - t3 > 115000) break
  }
  for (const st of statuses3) say(`[3] 状态轨迹: ${st}`)
  say(`[3] 失败卡: head="${done3?.failHead}" sections=${JSON.stringify(done3?.failSections)}`)
  assert('走不通:出现结构化失败卡', Boolean(done3))
  assert('走不通:标题明确', (done3?.failHead || '').includes('无法执行') || (done3?.failHead || '').includes('没跑通'))
  assert('走不通:含「具体原因」', (done3?.failSections || []).includes('具体原因'))
  assert('走不通:含「技术限制」或「潜在问题」', (done3?.failSections || []).some((s) => s === '技术限制' || s === '潜在问题'))
  assert('走不通:证据提到 scipy', Boolean(done3?.mentionsScipy))
  await scrollAndShot(cdp, 'chat-03-alt-fail')

  // ---- 收尾断言:上下文完整、无失败气泡、输入框恢复
  const final = await cdp.eval(CHAT_SNAP)
  say(`[final] 消息总数=${final.total} 用户=${final.userCount} 助手=${final.assistantCount} 失败气泡=${final.failedBubbles}`)
  assert('完整对话链保留(3 问 3 答)', final.userCount === 3 && final.assistantCount === 3)
  assert('无失败气泡', final.failedBubbles === 0)
  assert('输入框已清空、可继续提问', final.inputValue === '' && !final.busy)
  await scrollAndShot(cdp, 'chat-04-overview')

  say('\n================ 断言汇总 ================')
  let allOk = true
  for (const [name, pass] of okSteps) {
    say(`  [${pass ? 'ok' : 'FAIL'}] ${name}`)
    if (!pass) allOk = false
  }
  say('总判定: ' + (allOk ? 'PASS' : 'FAIL'))

  writeFileSync(`${TMP}/ui-chat-report.txt`, report.join('\n'))
  chrome.kill()
  process.exit(allOk ? 0 : 1)
}

main().catch((e) => {
  say('[ERROR] ' + (e && e.stack ? e.stack : e))
  writeFileSync(`${TMP}/ui-chat-report.txt`, report.join('\n'))
  chrome.kill()
  process.exit(1)
})
