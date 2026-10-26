// 浏览器真机验证:三 Agent 前端展示(分诊标签 / 审查标签 / 审查意见 / 修复轨迹)
import { spawn } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const CDP_PORT = 9336
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
    `--user-data-dir=${TMP}/chrome-mas-profile`,
    '--no-first-run',
    '--hide-scrollbars',
    '--window-size=1500,1000',
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

const SNAP_EXPR = `(() => {
  const card = document.querySelector('.card.diagnosis')
  const streamTag = document.querySelector('.tag-stream')
  const streamCard = streamTag ? streamTag.closest('.card') : null
  const streamTags = streamCard
    ? [...streamCard.querySelectorAll('.tag')].map(t => t.textContent.trim())
    : []
  if (!card) {
    return {
      phase: streamTag ? 'streaming' : 'none',
      statusMsg: streamTag ? streamTag.textContent.trim() : '',
      streamTags, tags: [], blockTitles: [], track: [], reviewItems: [],
    }
  }
  const tags = [...card.querySelectorAll('.tag')].map(t => t.textContent.trim())
  const blocks = [...card.querySelectorAll('.block')].map(b => ({
    title: b.querySelector('.block-title')?.textContent?.trim() || '',
  }))
  const track = [...card.querySelectorAll('.round-item')].map(it => it.innerText.trim().replace(/\\s+/g, ' '))
  const reviewItems = [...card.querySelectorAll('.review-list li')].map(li => li.textContent.trim())
  return {
    phase: streamTag ? 'streaming' : 'done',
    statusMsg: streamTag ? streamTag.textContent.trim() : '',
    streamTags, tags, blockTitles: blocks.map(b => b.title), track, reviewItems,
    hasApplyBtn: !!card.querySelector('.apply'),
  }
})()`

async function main() {
  const cdp = await CDP.connect(await getWsUrl())
  await cdp.send('Page.enable')
  await cdp.send('Runtime.enable')
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 1500, height: 1000, deviceScaleFactor: 1, mobile: false,
  })

  await cdp.send('Page.navigate', { url: APP_URL })
  for (let i = 0; i < 60; i++) {
    if (await cdp.eval(`!!document.querySelector('.btn.primary')`)) break
    await sleep(300)
  }
  await sleep(900)

  const badge = await cdp.eval(`document.querySelector('.ai-badge')?.textContent?.trim() || ''`)
  say(`[env] ${badge}`)

  // 载入除零示例
  await cdp.eval(`(() => {
    const sel = document.querySelector('.sample-select')
    const opt = [...sel.options].find(o => o.textContent.includes('除零'))
    sel.value = opt.value
    sel.dispatchEvent(new Event('change'))
    return opt.textContent
  })()`)
  await sleep(400)

  const t0 = Date.now()
  await cdp.eval(`(() => {
    const btn = [...document.querySelectorAll('button')].find(b => b.textContent.includes('运行诊断'))
    btn.click()
    return true
  })()`)

  const trace = []
  const statuses = []
  let done = false
  let shotDone = false

  while (Date.now() - t0 < 90000) {
    await sleep(300)
    const s = await cdp.eval(SNAP_EXPR)
    s.t = Number(((Date.now() - t0) / 1000).toFixed(1))
    trace.push(s)
    if (s.statusMsg && !statuses.includes(s.statusMsg)) statuses.push(s.statusMsg)

    if (s.phase === 'done' && s.hasApplyBtn) {
      await sleep(400)
      if (!shotDone) {
        await cdp.shot('mas-01-diagnosis')
        shotDone = true
      }
      done = true
      break
    }
  }

  const final = trace[trace.length - 1] || {}

  say('\n================ 状态文案轨迹 ================')
  for (const m of statuses) say(`  · ${m}`)

  say('\n================ 最终结果 ================')
  say(`  tags        = ${JSON.stringify(final.tags)}`)
  say(`  blockTitles = ${JSON.stringify(final.blockTitles)}`)
  say(`  track       = ${JSON.stringify(final.track)}`)
  say(`  reviewItems = ${JSON.stringify((final.reviewItems || []).map((x) => x.slice(0, 60)))}`)

  // 分诊是微秒级的(L1 命中),过程文案会被下一阶段覆盖;改为验证流式卡上的常驻标签
  const sawTriageTag = trace.some((s) =>
    (s.streamTags || []).some((t) => /语法|逻辑|环境依赖|数据/.test(t)))
  const sawReview = statuses.some((m) => m.includes('审查'))
  const hasTriageTag = (final.tags || []).some((t) => /语法|逻辑|环境依赖|数据/.test(t))
  const hasReviewTag = (final.tags || []).some((t) => t.includes('审查'))

  const checks = [
    ['完成态出现诊断卡', done],
    ['流式过程中出现分诊标签', sawTriageTag],
    ['状态文案出现 >= 3 种阶段', statuses.length >= 3],
    ['状态轨迹含审查文案', sawReview],
    ['结果标签含分诊结论', hasTriageTag],
    ['结果标签含审查结论', hasReviewTag],
    ['有「修复后的代码」块', (final.blockTitles || []).includes('修复后的代码')],
  ]

  say('\n================ 断言 ================')
  let allOk = true
  for (const [name, pass] of checks) {
    say(`  [${pass ? 'ok' : 'FAIL'}] ${name}`)
    if (!pass) allOk = false
  }
  say('总判定: ' + (allOk && done ? 'PASS' : 'FAIL'))

  writeFileSync(`${TMP}/ui-mas-report.txt`, report.join('\n'))
  chrome.kill()
  process.exit(allOk && done ? 0 : 1)
}

main().catch((e) => {
  say('[ERROR] ' + (e && e.stack ? e.stack : e))
  writeFileSync(`${TMP}/ui-mas-report.txt`, report.join('\n'))
  chrome.kill()
  process.exit(1)
})
