// 浏览器真机验证:流式诊断 UI(轮询捕获状态轨迹 + 关键截图)
// 运行:node ui-stream-verify.mjs
import { spawn } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const CDP_PORT = 9333
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

const chrome = spawn(
  CHROME,
  [
    '--headless=new',
    `--remote-debugging-port=${CDP_PORT}`,
    `--user-data-dir=${TMP}/chrome-stream-profile`,
    '--no-first-run',
    '--hide-scrollbars',
    '--window-size=1500,950',
    'about:blank',
  ],
  { stdio: 'ignore' },
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
      ws.onerror = (e) => rej(new Error('ws error'))
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

const SNAP_EXPR = `(() => {
  const card = document.querySelector('.card.diagnosis')
  if (!card) return { phase: 'none' }
  const msgEl = card.querySelector('.tag-stream')
  const tags = [...card.querySelectorAll('.tag')].map(t => t.textContent.trim())
  const text = card.innerText || ''
  const blocks = [...card.querySelectorAll('.block')].map(b => {
    const title = b.querySelector('.block-title')?.textContent?.trim() || ''
    const body = b.querySelector('p, pre, ol')?.textContent || ''
    return { title, len: body.length }
  })
  return {
    phase: msgEl ? 'streaming' : 'done',
    statusMsg: msgEl ? msgEl.textContent.trim() : '',
    tags,
    textLen: text.length,
    blocks,
    hasApplyBtn: !!card.querySelector('.apply'),
    hasVerified: tags.some(t => t.includes('已通过真实验证')),
    hasCaret: !!card.querySelector('.caret'),
  }
})()`

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
  if (!badge.includes('AI 已就绪')) {
    say('[FAIL] 后端未就绪,无法进行流式验证')
    writeFileSync(`${TMP}/ui-stream-report.txt`, report.join('\n'))
    process.exit(1)
  }

  // 载入示例:选"除零错误"示例
  await cdp.eval(`(() => {
    const sel = document.querySelector('.sample-select')
    const opt = [...sel.options].find(o => o.textContent.includes('除零'))
    sel.value = opt.value
    sel.dispatchEvent(new Event('change'))
    return true
  })()`)
  await sleep(300)

  // 点击「运行诊断」,开始轮询
  const t0 = Date.now()
  await cdp.eval(`(() => {
    const btn = [...document.querySelectorAll('button')].find(b => b.textContent.includes('运行诊断'))
    btn.click()
    return true
  })()`)

  const trace = []
  let shotStreaming = false
  let shotDone = false
  let lastLen = -1
  let done = false

  while (Date.now() - t0 < 60000) {
    await sleep(280)
    const s = await cdp.eval(SNAP_EXPR)
    const t = ((Date.now() - t0) / 1000).toFixed(1)
    s.t = Number(t)
    trace.push(s)

    if (s.phase === 'streaming') {
      if (!shotStreaming && s.textLen > 120) {
        await cdp.shot('stream-01-midway')
        shotStreaming = true
        say(`[${t}s] 截屏(流式中): status="${s.statusMsg}" textLen=${s.textLen} caret=${s.hasCaret}`)
      }
    }
    if (s.phase === 'done' && s.hasApplyBtn) {
      if (!shotDone) {
        await sleep(400)
        await cdp.shot('stream-02-done')
        shotDone = true
        say(`[${t}s] 截屏(完成): tags=${JSON.stringify(s.tags)}`)
      }
      done = true
      break
    }
    if (s.phase === 'none' && Date.now() - t0 > 15000) {
      say('[FAIL] 15s 后仍未出现诊断卡')
      break
    }
  }

  // ---- 轨迹分析 ----
  say('\n================ 状态轨迹 ================')
  let prevLen = -1
  const seenMsgs = new Set()
  let sawIncrement = false
  for (const s of trace) {
    if (s.phase === 'streaming') {
      if (s.statusMsg && !seenMsgs.has(s.statusMsg)) {
        seenMsgs.add(s.statusMsg)
        say(`[${s.t}s] STATUS  ${s.statusMsg}`)
      }
      if (s.textLen > prevLen && prevLen >= 0 && s.textLen - prevLen > 20) {
        sawIncrement = true
      }
      prevLen = s.textLen
    }
  }
  const final = trace[trace.length - 1]
  say(`\n最终状态: phase=${final?.phase} tags=${JSON.stringify(final?.tags)}`)
  say(`streaming 快照数: ${trace.filter((s) => s.phase === 'streaming').length} / 总 ${trace.length}`)
  say(`文本长度递增推进: ${sawIncrement ? '是' : '否'}`)

  const okSteps = []
  okSteps.push(['流式卡出现(streaming 快照 >= 3)', trace.filter((s) => s.phase === 'streaming').length >= 3])
  okSteps.push(['状态文案出现过至少 2 种', seenMsgs.size >= 2])
  okSteps.push(['文本是渐进出现的', sawIncrement || trace.some((s) => s.phase === 'streaming' && s.textLen > 100)])
  okSteps.push(['完成态: 已通过真实验证', !!final?.hasVerified])
  okSteps.push(['完成态: 有一键应用按钮', !!final?.hasApplyBtn])
  okSteps.push(['流式中光标出现过', trace.some((s) => s.hasCaret)])

  say('\n================ 断言 ================')
  let allOk = true
  for (const [name, pass] of okSteps) {
    say(`  [${pass ? 'ok' : 'FAIL'}] ${name}`)
    if (!pass) allOk = false
  }
  say('总判定: ' + (allOk && done ? 'PASS' : 'FAIL'))

  writeFileSync(`${TMP}/ui-stream-report.txt`, report.join('\n'))
  chrome.kill()
  process.exit(allOk && done ? 0 : 1)
}

main().catch((e) => {
  say('[ERROR] ' + (e && e.stack ? e.stack : e))
  writeFileSync(`${TMP}/ui-stream-report.txt`, report.join('\n'))
  chrome.kill()
  process.exit(1)
})
