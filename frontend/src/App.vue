<template>
  <div class="page">
    <header class="topbar">
      <div class="brand">
        <div class="logo">CD</div>
        <div class="titles">
          <h1>CodeDoctor</h1>
          <p>代码报错诊断助手 · 先真实运行一遍,再用大白话讲清楚</p>
        </div>
      </div>
      <div class="toolbar">
        <span class="ai-badge" :class="badgeClass">
          <i class="dot"></i>{{ aiStatusText }}
        </span>
        <button class="btn ghost" @click="openKb">
          案例文档<span v-if="kbCount" class="kb-count">{{ kbCount }}</span>
        </button>
        <select v-model="selectedSample" class="sample-select" @change="loadSample">
          <option value="">载入示例代码…</option>
          <option v-for="s in samples" :key="s.name" :value="s.name">{{ s.name }}</option>
        </select>
        <button class="btn primary" :disabled="busy || chatBusy" @click="runAndDiagnose">
          {{ busy ? '诊断中…' : '运行诊断' }}
        </button>
      </div>
    </header>

    <main class="layout">
      <section class="editor-pane">
        <MonacoEditor ref="editorRef" :initial-value="initialCode" />
      </section>

      <aside class="side-pane">
        <div v-if="phase === 'idle'" class="card welcome">
          <h2>从一段跑不通的代码开始</h2>
          <p>
            把程序粘贴到左边的编辑器,点「运行诊断」。CodeDoctor 会真实执行它、复现报错,
            然后告诉你哪里错了、怎么改。
          </p>
          <ul class="feature-list">
            <li>真实运行 —— 报错是真跑出来的,不是猜的</li>
            <li>大白话解释 —— 像助教一样讲给你听</li>
            <li>交付前验证 —— 修复代码先在服务端真跑一遍,跑通才交给你一键填回</li>
          </ul>
          <p class="hint">想快速体验?点右上角「载入示例代码」,选一个常见报错。</p>
        </div>

        <template v-else>
          <div class="card status" :class="statusClass">
            <div class="status-title">{{ statusTitle }}</div>
            <div v-if="statusDetail" class="status-detail">{{ statusDetail }}</div>
          </div>

          <div v-if="phase === 'failed'" class="card">
            <div class="card-title">出错了</div>
            <p class="muted">{{ failureMessage }}</p>
          </div>

          <div v-if="runResult" class="card">
            <div class="card-title">程序输出</div>
            <pre v-if="runResult.stdout" class="terminal">{{ runResult.stdout }}</pre>
            <p v-else class="muted">(程序没有产生输出)</p>
          </div>

          <div v-if="showStderr" class="card">
            <div class="card-title">真实报错(终端原文)</div>
            <pre class="terminal error-text">{{ runResult.stderr }}</pre>
          </div>

          <div v-if="stream" class="card diagnosis">
            <div class="card-title">
              诊断结果
              <span class="tag tag-stream">
                <i class="pulse-dot"></i>{{ stream.msg }}
              </span>
              <span v-if="streamTriageLabel" class="tag tag-triage">{{ streamTriageLabel }}</span>
              <span v-if="stream.round > 1" class="tag tag-round">第 {{ stream.round }} 轮</span>
            </div>

            <div v-if="stream.history.length" class="round-log">
              <div
                v-for="h in stream.history"
                :key="h.round"
                class="round-item"
                :class="h.ok ? 'pass' : 'fail'"
              >
                <i class="round-dot"></i>第 {{ h.round }} 轮:{{ h.msg }}
              </div>
            </div>

            <div v-if="stream.explain" class="block">
              <div class="block-title">发生了什么</div>
              <p>{{ stream.explain }}<span v-if="stream.activeField === 'explain'" class="caret"></span></p>
            </div>
            <div v-if="stream.reason" class="block">
              <div class="block-title">为什么会错</div>
              <p>{{ stream.reason }}<span v-if="stream.activeField === 'reason'" class="caret"></span></p>
            </div>
            <div v-if="stream.fix_points.length" class="block">
              <div class="block-title">怎么改</div>
              <ol class="fix-list">
                <li v-for="(point, index) in stream.fix_points" :key="index">{{ point }}</li>
              </ol>
            </div>
            <div v-if="stream.fixed_code" class="block">
              <div class="block-title">修复后的代码</div>
              <pre class="terminal fixed">{{ stripFence(stream.fixed_code) }}</pre>
            </div>
          </div>

          <div v-if="diag" class="card diagnosis">
            <div class="card-title">
              诊断结果
              <span class="tag" :class="diag.source === 'ai' ? 'tag-ai' : 'tag-rule'">
                {{ diag.source === 'ai' ? diag.model || 'AI' : '规则库' }}
              </span>
              <span v-if="diag.verified" class="tag tag-verified">
                已通过真实验证 · 第 {{ diag.verify_rounds }} 轮
              </span>
              <span v-else-if="diag.fixed_code" class="tag tag-unverified">
                修复未通过验证
              </span>
              <span v-if="triageLabel" class="tag tag-triage">{{ triageLabel }}</span>
              <span v-if="reviewBadge" class="tag" :class="reviewBadge.cls">{{ reviewBadge.text }}</span>
              <span
                v-if="diag.case_sources && diag.case_sources.length"
                class="tag tag-case"
                :title="caseSourceTitle"
              >
                参考案例 ×{{ diag.case_sources.length }}
              </span>
            </div>
            <div class="block">
              <div class="block-title">发生了什么</div>
              <p>{{ diag.explain }}</p>
            </div>
            <div v-if="diag.reason" class="block">
              <div class="block-title">为什么会错</div>
              <p>{{ diag.reason }}</p>
            </div>
            <div v-if="diag.fix_points && diag.fix_points.length" class="block">
              <div class="block-title">怎么改</div>
              <ol class="fix-list">
                <li v-for="(point, index) in diag.fix_points" :key="index">{{ point }}</li>
              </ol>
            </div>
            <div v-if="diag.fixed_code" class="block">
              <div class="block-title">修复后的代码</div>
              <pre class="terminal fixed">{{ diag.fixed_code }}</pre>
              <p v-if="diag.verified && diag.verify_output" class="verify-note">
                验证运行输出:{{ diag.verify_output }}
              </p>
              <button class="btn primary apply" :disabled="busy" @click="applyFix">
                一键应用到编辑器,并重新运行
              </button>
            </div>
            <div v-if="reviewIssues.length" class="block review-block">
              <div class="block-title">审查意见</div>
              <ul class="review-list">
                <li v-for="(issue, i) in reviewIssues" :key="i">{{ issue }}</li>
              </ul>
              <p v-if="diag.review && diag.review.overridden" class="review-hint">
                这一版已通过真实运行,但审查还有疑虑 —— 先按上面的意见看看是否真的解决了你的问题。
              </p>
            </div>
            <div v-if="attemptTrack.length" class="block">
              <div class="block-title">修复轨迹</div>
              <div class="round-track">
                <div v-for="(a, i) in attemptTrack" :key="i" class="round-item">
                  <i class="round-dot" :class="a.ok ? 'dot-ok' : 'dot-fail'"></i>
                  <span class="round-name">
                    第 {{ a.round }} 轮 · {{ attemptLabel(a) }}
                  </span>
                  <span v-if="a.error" class="round-err">{{ a.error }}</span>
                </div>
              </div>
            </div>
            <p v-if="diag.notice" class="notice">{{ diag.notice }}</p>
            <p v-if="diag.ai_error" class="notice warn">{{ diag.ai_error }}</p>
          </div>

          <div v-if="chatAvailable" class="card chat-card">
            <div class="card-title">
              继续追问
              <span class="chat-sub">有疑问直接问,或提出你想试的另一种改法</span>
            </div>
            <div ref="chatListRef" class="chat-list">
              <div v-if="!chatMessages.length" class="chat-empty">
                <p v-if="diag">
                  可以问我「为什么报错」「怎么避免」,也可以提出你自己的修改思路 ——
                  比如「为什么不用 try/except?」。我会真的改一版、真实运行验证,再告诉你行不行。
                </p>
                <p v-else>
                  程序跑通了,还想聊点什么都可以:问问这段代码还能怎么改进,或者提出你想试的另一种写法 ——
                  比如「为什么不先做输入检查?」。我会真的改一版、真实运行验证,再告诉你行不行。
                </p>
                <div class="chat-suggestions">
                  <button
                    v-for="s in chatSuggestions"
                    :key="s"
                    class="chip"
                    @click="useSuggestion(s)"
                  >
                    {{ s }}
                  </button>
                </div>
              </div>

              <div v-for="(m, i) in chatMessages" :key="i" class="chat-msg" :class="m.role">
                <div class="bubble" :class="{ 'bubble-failed': m.failed }">
                  <div v-if="m.streaming && m.status" class="chat-status">
                    <i class="pulse-dot"></i>{{ m.status }}
                  </div>
                  <p v-if="m.text" class="chat-text">{{ m.text }}<span
                    v-if="m.streaming && !m.payload"
                    class="caret"
                  ></span></p>

                  <div
                    v-if="m.payload && m.payload.kind === 'alt_success'"
                    class="alt-card alt-ok"
                  >
                    <div class="alt-head">替代方案真实运行通过 ✓</div>
                    <div class="alt-line">
                      方案:{{ m.payload.approach }}
                      <span v-if="m.payload.rounds > 1"> · 第 {{ m.payload.rounds }} 轮调整后通过</span>
                    </div>
                    <pre class="terminal fixed">{{ m.payload.code }}</pre>
                    <p v-if="m.payload.stdout" class="verify-note">验证运行输出:{{ m.payload.stdout }}</p>
                    <button class="btn ghost chip-btn" @click="fillEditor(m.payload.code)">
                      填回编辑器
                    </button>
                  </div>

                  <div
                    v-if="m.payload && m.payload.kind === 'alt_fail'"
                    class="alt-card alt-fail"
                  >
                    <div class="alt-head">
                      这个方案{{ m.payload.infeasible ? '无法执行' : '没跑通' }}
                    </div>
                    <div class="alt-line">方案:{{ m.payload.approach }}</div>
                    <div v-if="m.payload.attempts && m.payload.attempts.length" class="alt-attempts">
                      <div v-for="a in m.payload.attempts" :key="a.round" class="alt-attempt">
                        <i class="round-dot" :class="a.ok ? 'dot-ok' : 'dot-fail'"></i>第 {{ a.round }} 轮:{{ a.error || '未通过' }}
                      </div>
                    </div>
                    <div v-if="m.payload.reasons && m.payload.reasons.length" class="alt-section">
                      <div class="alt-section-title">具体原因</div>
                      <ul>
                        <li v-for="(r, j) in m.payload.reasons" :key="j">{{ r }}</li>
                      </ul>
                    </div>
                    <div v-if="m.payload.limits && m.payload.limits.length" class="alt-section">
                      <div class="alt-section-title">技术限制</div>
                      <ul>
                        <li v-for="(r, j) in m.payload.limits" :key="j">{{ r }}</li>
                      </ul>
                    </div>
                    <div v-if="m.payload.issues && m.payload.issues.length" class="alt-section">
                      <div class="alt-section-title">潜在问题</div>
                      <ul>
                        <li v-for="(r, j) in m.payload.issues" :key="j">{{ r }}</li>
                      </ul>
                    </div>
                    <div v-if="m.payload.suggestion" class="alt-section">
                      <div class="alt-section-title">建议</div>
                      <p class="alt-suggestion">{{ m.payload.suggestion }}</p>
                    </div>
                  </div>
                </div>
              </div>
            </div>
            <div class="chat-input-row">
              <textarea
                v-model="chatInput"
                class="chat-input"
                rows="1"
                :disabled="chatBusy"
                placeholder="继续提问,或说说你想试的另一种改法…(Enter 发送,Shift+Enter 换行)"
                @keydown.enter.exact.prevent="sendChat"
              ></textarea>
              <button
                class="btn primary chat-send"
                :disabled="chatBusy || !chatInput.trim()"
                @click="sendChat"
              >
                {{ chatBusy ? '处理中…' : '发送' }}
              </button>
            </div>
          </div>
        </template>
      </aside>
    </main>

    <div v-if="kbOpen" class="modal-mask" @click.self="closeKb">
      <div class="modal">
        <div class="modal-head">
          <h2>案例文档</h2>
          <p>上传教学资料(支持 Markdown / txt / Word / PDF),自动切分、向量化入库;诊断时会自动参考这些案例。</p>
          <button class="modal-close" @click="closeKb">✕</button>
        </div>

        <label
          class="upload-zone"
          :class="{ dragging: dragOver, busy: kbBusy }"
          @dragover.prevent="dragOver = true"
          @dragleave="dragOver = false"
          @drop.prevent="onDrop"
        >
          <input
            type="file"
            accept=".md,.markdown,.txt,.docx,.pdf"
            multiple
            hidden
            @change="onPick"
          />
          <div v-if="kbBusy" class="uz-title">正在上传并建立向量索引…</div>
          <template v-else>
            <div class="uz-title">拖拽文件到这里,或点击选择</div>
            <div class="uz-sub">支持 Markdown / txt / Word / PDF,单个文件不超过 10MB</div>
          </template>
        </label>

        <p v-if="kbError" class="kb-msg err">{{ kbError }}</p>
        <p v-if="kbNotice" class="kb-msg ok">{{ kbNotice }}</p>

        <div class="doc-list">
          <div class="doc-list-title">已入库文档({{ kbDocs.length }})</div>
          <div v-if="!kbDocs.length" class="muted">还没有上传案例文档</div>
          <div v-for="d in kbDocs" :key="d.doc_id" class="doc-item">
            <div class="doc-main">
              <div class="doc-name">{{ d.filename }}</div>
              <div class="doc-meta">{{ d.chunks }} 个知识块 · {{ d.uploaded_at }}</div>
            </div>
            <button
              class="doc-del"
              :class="{ confirm: pendingDelete === d.doc_id }"
              :disabled="kbBusy"
              @click="removeDoc(d)"
            >
              {{ pendingDelete === d.doc_id ? '确认删除?' : '删除' }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, ref } from 'vue'

import MonacoEditor from './components/MonacoEditor.vue'
import { chatStream, deleteKbDoc, diagnoseStream, fetchHealth, fetchKbDocs, runCode, uploadKbDoc } from './api'

const initialCode = `# 计算本次考试的平均分
scores = [88, 92, 79, 95]
total = sum(scores)
count = 0                 # 注意这一行
average = total / count
print("平均分是:", average)
`

const samples = [
  {
    name: '除零错误 ZeroDivisionError',
    code: `# 计算本次考试的平均分
scores = [88, 92, 79, 95]
total = sum(scores)
count = 0                 # 这里应该是 len(scores)
average = total / count
print("平均分是:", average)
`,
  },
  {
    name: '变量名写错了 NameError',
    code: `name = "小明"
age = 18
print("我叫" + nmae)
print("今年", age, "岁")
`,
  },
  {
    name: '缩进问题 IndentationError',
    code: `for i in range(3):
print("第", i, "次打卡")
`,
  },
  {
    name: '下标越界 IndexError',
    code: `fruits = ["苹果", "香蕉", "橘子"]
print("我最喜欢的水果是:", fruits[3])
`,
  },
  {
    name: '类型不匹配 TypeError',
    code: `age = "18"
next_year = age + 1
print("明年就", next_year, "岁了")
`,
  },
  {
    name: '死循环(超时保护)',
    code: `count = 0
while count < 10:
    pass                  # 忘了让 count 增加,这个循环永远停不下来
`,
  },
]

const editorRef = ref(null)
const phase = ref('idle')
const busy = ref(false)
const runResult = ref(null)
const diag = ref(null)
const stream = ref(null)   // 流式诊断的实时状态(进行中);完成后清空、切到 diag
const failureMessage = ref('')
const selectedSample = ref('')

// ---- 多轮追问 ----
const diagContext = ref(null)    // 运行时上下文(含跑通场景的上一轮诊断),首次追问时交给后端建会话
const chatSessionId = ref('')
const chatMessages = ref([])     // [{role, text, status, streaming, payload, failed}]
const chatInput = ref('')
const chatBusy = ref(false)
const chatListRef = ref(null)

// 追问区出现条件:诊断完成 / 或程序跑通(两种状态都保留追问能力)
const chatAvailable = computed(
  () => (phase.value === 'diagnosed' && Boolean(diag.value))
    || (phase.value === 'ran' && Boolean(runResult.value && runResult.value.ok)),
)

// 快捷示例:按当前场景给更贴切的问题
const chatSuggestions = computed(() => {
  if (diag.value) {
    return [
      '这个错误常见吗?以后怎么避免?',
      '为什么不用 sum(scores) / len(scores) 直接算?',
      '为什么不用 try/except 包起来试试?',
    ]
  }
  return [
    '这段代码还有什么可以改进的地方?',
    '为什么不先做输入检查再计算?',
    '如果 scores 是空列表,这段代码会怎样?',
  ]
})

const backendUp = ref(true)
const aiConfigured = ref(false)
const aiModel = ref('')

const kbOpen = ref(false)
const kbDocs = ref([])
const kbCount = ref(0)
const kbBusy = ref(false)
const kbError = ref('')
const kbNotice = ref('')
const dragOver = ref(false)
const pendingDelete = ref('')

const badgeClass = computed(() => {
  if (!backendUp.value) return 'bad'
  return aiConfigured.value ? 'ok' : 'warn'
})

const aiStatusText = computed(() => {
  if (!backendUp.value) return '后端未连接'
  if (aiConfigured.value) return `AI 已就绪 · ${aiModel.value}`
  return 'AI 未配置 · 规则库模式'
})

// ---- 三 Agent(分诊 / 审查)展示 ----
const CATEGORY_LABELS = {
  syntax: '语法', logic: '逻辑', environment: '环境依赖', data: '数据', unknown: '未分类',
}
const STRATEGY_LABELS = {
  minimal_fix: '最小改动', logic_rewrite: '重写逻辑', env_guide: '环境指引',
}

function triageText(t) {
  if (!t || !t.error_category || t.source === 'fallback') return ''
  const cat = CATEGORY_LABELS[t.error_category]
  if (!cat) return ''
  const strategy = STRATEGY_LABELS[t.repair_strategy]
  return strategy ? `${cat} · ${strategy}` : cat
}

const triageLabel = computed(() => triageText((diag.value && diag.value.triage) || null))
const streamTriageLabel = computed(() => triageText((stream.value && stream.value.triage) || null))

const reviewBadge = computed(() => {
  const r = (diag.value && diag.value.review) || null
  if (!r || !r.verdict) return null
  if (r.overridden) return { text: '审查有疑虑 · 已放行', cls: 'tag-review-warn' }
  if (r.verdict === 'pass') return { text: '审查通过', cls: 'tag-review-pass' }
  if (r.verdict === 'reject') return { text: '审查认为方案不可行', cls: 'tag-review-warn' }
  return { text: '审查打回', cls: 'tag-review-warn' }
})

const reviewIssues = computed(() => {
  const r = (diag.value && diag.value.review) || null
  if (!r || !Array.isArray(r.issues)) return []
  return r.issues.filter((x) => String(x || '').trim())
})

const attemptTrack = computed(() => {
  const list = (diag.value && diag.value.attempts) || []
  // 只有一条且通过时不显示,避免噪音;出现过审查打回则必须展示
  if (list.length <= 1 && !list.some((a) => a && a.gate === 'critic')) return []
  return list
})

function attemptLabel(a) {
  if (!a) return ''
  if (a.gate === 'critic') return '审查打回'
  return a.ok ? '验证通过' : '验证未通过'
}

// 流式增量里模型偶尔会带上 markdown 围栏,渲染前先去掉
function stripFence(text) {
  return String(text || '').replace(/^```[A-Za-z0-9_+.\-]*\s*\n?/, '')
}

const caseSourceTitle = computed(() => {
  if (!diag.value || !diag.value.case_sources) return ''
  return diag.value.case_sources
    .map((c) => `${c.filename || '未知文档'}${c.title ? ' · ' + c.title : ''}`)
    .join('\n')
})

const showStderr = computed(() => Boolean(runResult.value && runResult.value.stderr && runResult.value.stderr.trim()))

const statusClass = computed(() => {
  switch (phase.value) {
    case 'running':
    case 'diagnosing':
      return 'running'
    case 'ran':
    case 'diagnosed':
      return runResult.value && runResult.value.ok ? 'ok' : 'err'
    default:
      return ''
  }
})

const statusTitle = computed(() => {
  switch (phase.value) {
    case 'running':
      return '正在真实运行你的代码…'
    case 'diagnosing':
      return '已捕获真实报错 · 正在诊断并验证修复'
    case 'ran':
    case 'diagnosed': {
      const result = runResult.value
      if (result && result.ok) return '程序跑通了,没有发现报错'
      if (result && result.timed_out) return '程序超时,已被自动终止'
      return '程序运行出错'
    }
    default:
      return ''
  }
})

const statusDetail = computed(() => {
  const result = runResult.value
  if (!result) return ''
  if (result.timed_out) return '疑似死循环或长时间等待,请检查循环的退出条件'
  if (result.ok) return '退出码 0,程序正常结束'
  const error = result.error
  if (error) {
    const parts = [error.type]
    if (error.line) parts.push(`第 ${error.line} 行`)
    if (error.message) parts.push(error.message)
    return parts.join(' · ')
  }
  return `程序以退出码 ${result.exit_code} 结束`
})

function resetResult() {
  runResult.value = null
  diag.value = null
  stream.value = null
  failureMessage.value = ''
  diagContext.value = null
  chatSessionId.value = ''
  chatMessages.value = []
  chatInput.value = ''
  chatBusy.value = false
  if (editorRef.value) editorRef.value.clearErrorMark()
}

function loadSample() {
  const sample = samples.find((item) => item.name === selectedSample.value)
  if (!sample) return
  editorRef.value.setCode(sample.code)
  phase.value = 'idle'
  resetResult()
}

function newStreamState() {
  return {
    stage: 'connect',
    msg: '正在连接诊断服务…',
    round: 1,
    triage: null,
    activeField: '',
    explain: '',
    reason: '',
    fix_points: [],
    fixed_code: '',
    history: [],
  }
}

function applyStreamEvent(evt) {
  const s = stream.value
  if (!s) return
  if (evt.type === 'status') {
    if (evt.msg) s.msg = evt.msg
    if (evt.stage) s.stage = evt.stage
    if (evt.triage) s.triage = evt.triage
    const round = Number(evt.round || 0)
    if (round > s.round) {
      // 进入新一轮:上一轮诊断已被验证否决,清空重写
      s.round = round
      s.explain = ''
      s.reason = ''
      s.fix_points = []
      s.fixed_code = ''
      s.activeField = ''
    }
    if (evt.stage === 'verify_result') {
      s.history.push({ round: evt.round || s.round, ok: Boolean(evt.ok), msg: evt.msg || '' })
    }
  } else if (evt.type === 'field') {
    s.activeField = evt.phase === 'start' ? evt.field : ''
  } else if (evt.type === 'delta') {
    if (evt.field in s && typeof s[evt.field] === 'string') {
      s[evt.field] += evt.text
    }
  } else if (evt.type === 'item') {
    if (evt.field === 'fix_points') s.fix_points.push(evt.text)
  }
}

async function runAndDiagnose() {
  if (!editorRef.value) return
  const code = editorRef.value.getValue()
  if (!code.trim()) return

  // 暂存上一轮的诊断上下文:若这次跑通,追问会话会带上它(支撑「为什么这样改就好了」类追问)
  const prevDiag = diag.value
  const prevContext = diagContext.value

  busy.value = true
  resetResult()
  phase.value = 'running'

  try {
    const result = await runCode(code)
    runResult.value = result
    diagContext.value = {
      code,
      stdout: result.stdout || '',
      stderr: result.stderr || '',
      error: result.error || null,
      timed_out: Boolean(result.timed_out),
      prior: result.ok && prevDiag
        ? { code: (prevContext && prevContext.code) || '', stderr: (prevContext && prevContext.stderr) || '', diagnosis: prevDiag }
        : null,
    }
    if (result.error && result.error.line) {
      editorRef.value.markErrorLine(result.error.line)
    }
    phase.value = 'ran'

    if (!result.ok) {
      phase.value = 'diagnosing'
      stream.value = newStreamState()
      let finalData = null
      await diagnoseStream(
        {
          code,
          stdout: result.stdout,
          stderr: result.stderr,
          error: result.error,
          timed_out: result.timed_out,
        },
        (evt) => {
          if (evt.type === 'result') {
            finalData = evt.data
            return
          }
          if (evt.type === 'error') {
            throw new Error(evt.message || '诊断服务出错')
          }
          applyStreamEvent(evt)
        },
      )
      stream.value = null
      if (finalData) {
        diag.value = finalData
        if (diag.value.error_line) {
          editorRef.value.markErrorLine(diag.value.error_line)
        }
      }
      phase.value = 'diagnosed'
      chatSessionId.value = makeId()
    } else {
      // 程序跑通:追问区照常保留,开启新会话
      chatSessionId.value = makeId()
    }
  } catch (error) {
    stream.value = null
    phase.value = 'failed'
    failureMessage.value = error && error.message ? error.message : String(error)
  } finally {
    busy.value = false
  }
}

function applyFix() {
  if (!diag.value || !diag.value.fixed_code) return
  editorRef.value.setCode(diag.value.fixed_code)
  runAndDiagnose()
}

// ---- 多轮追问 ----

function makeId() {
  if (window.crypto && typeof window.crypto.randomUUID === 'function') {
    return window.crypto.randomUUID()
  }
  return 'sess-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8)
}

function scrollChat() {
  nextTick(() => {
    const el = chatListRef.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

function useSuggestion(text) {
  chatInput.value = text
}

function fillEditor(code) {
  if (!code) return
  editorRef.value.setCode(code)
}

async function sendChat() {
  const text = chatInput.value.trim()
  if (!text || chatBusy.value || !chatAvailable.value || !diagContext.value) return
  if (!chatSessionId.value) chatSessionId.value = makeId()

  chatBusy.value = true
  chatInput.value = ''
  chatMessages.value.push({ role: 'user', text })
  chatMessages.value.push({
    role: 'assistant', text: '', status: '正在连接…', streaming: true, payload: null, failed: false,
  })
  const live = chatMessages.value[chatMessages.value.length - 1]
  scrollChat()

  try {
    await chatStream(
      {
        session_id: chatSessionId.value,
        message: text,
        context: { ...diagContext.value, diagnosis: diag.value || null },
        history: chatMessages.value.slice(0, -2).map((m) => ({ role: m.role, text: m.text })),
      },
      (evt) => {
        if (evt.type === 'status') {
          if (evt.msg) live.status = evt.msg
        } else if (evt.type === 'delta') {
          if (evt.text) live.text += evt.text
        } else if (evt.type === 'result') {
          const data = evt.data || {}
          if (data.kind === 'alt_success' || data.kind === 'alt_fail') {
            live.payload = data
            if (!live.text && data.summary) live.text = data.summary
            if (!live.text && data.text) live.text = data.text
          } else if (data.kind === 'text' && !live.text && data.text) {
            live.text = data.text
          }
        } else if (evt.type === 'error') {
          live.failed = true
          live.text = (live.text ? live.text + '\n' : '') + (evt.message || '服务暂时不可用')
        }
        scrollChat()
      },
    )
  } catch (error) {
    live.failed = true
    live.text = (live.text ? live.text + '\n' : '')
      + '这次追问没有完成:' + (error && error.message ? error.message : String(error))
  } finally {
    live.streaming = false
    live.status = ''
    chatBusy.value = false
    scrollChat()
  }
}

async function openKb() {
  kbOpen.value = true
  kbError.value = ''
  kbNotice.value = ''
  await refreshKbDocs()
}

function closeKb() {
  kbOpen.value = false
  pendingDelete.value = ''
}

async function refreshKbDocs() {
  try {
    const res = await fetchKbDocs()
    kbDocs.value = res.docs || []
    kbCount.value = kbDocs.value.length
  } catch (error) {
    kbError.value = error.message || String(error)
  }
}

async function uploadFiles(files) {
  const list = Array.from(files || [])
  if (!list.length) return
  kbBusy.value = true
  kbError.value = ''
  kbNotice.value = ''
  const done = []
  try {
    for (const file of list) {
      const res = await uploadKbDoc(file)
      if (!res.ok) {
        kbError.value = `${file.name}:${res.error || '上传失败'}`
        continue
      }
      done.push(`${file.name}(${res.chunks} 块)`)
    }
    if (done.length) kbNotice.value = `已入库:${done.join('、')}`
    await refreshKbDocs()
  } catch (error) {
    kbError.value = error.message || String(error)
  } finally {
    kbBusy.value = false
  }
}

function onPick(event) {
  uploadFiles(event.target.files)
  event.target.value = ''
}

function onDrop(event) {
  dragOver.value = false
  uploadFiles(event.dataTransfer.files)
}

async function removeDoc(doc) {
  if (pendingDelete.value !== doc.doc_id) {
    pendingDelete.value = doc.doc_id
    setTimeout(() => {
      if (pendingDelete.value === doc.doc_id) pendingDelete.value = ''
    }, 3000)
    return
  }
  pendingDelete.value = ''
  kbBusy.value = true
  kbError.value = ''
  try {
    await deleteKbDoc(doc.doc_id)
    await refreshKbDocs()
  } catch (error) {
    kbError.value = error.message || String(error)
  } finally {
    kbBusy.value = false
  }
}

onMounted(async () => {
  try {
    const health = await fetchHealth()
    backendUp.value = true
    aiConfigured.value = Boolean(health.ai_configured)
    aiModel.value = health.model || ''
    kbCount.value = (health.kb_cases && health.kb_cases.docs) || 0
  } catch {
    backendUp.value = false
  }
})
</script>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
}

.topbar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 10px 20px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.logo {
  width: 40px;
  height: 40px;
  border-radius: 10px;
  background: var(--primary);
  color: #fff;
  font-weight: 600;
  font-size: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
}

.titles h1 {
  margin: 0;
  font-size: 17px;
  line-height: 1.3;
}

.titles p {
  margin: 0;
  font-size: 12px;
  color: var(--text-3);
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 0 0 auto;
}

.ai-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  padding: 5px 10px;
  border-radius: 999px;
  background: #f1f3f6;
  color: var(--text-2);
  white-space: nowrap;
}

.ai-badge .dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #c2c8d0;
}

.ai-badge.ok {
  background: var(--green-bg);
  color: var(--green);
}

.ai-badge.ok .dot {
  background: var(--green);
}

.ai-badge.warn {
  background: var(--amber-bg);
  color: var(--amber);
}

.ai-badge.warn .dot {
  background: var(--amber);
}

.ai-badge.bad {
  background: var(--red-bg);
  color: var(--red);
}

.ai-badge.bad .dot {
  background: var(--red);
}

.sample-select {
  font-size: 13px;
  padding: 7px 10px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: #fff;
  color: var(--text);
  outline: none;
  max-width: 220px;
}

.btn {
  font-size: 14px;
  font-weight: 500;
  padding: 8px 18px;
  border-radius: 8px;
  border: none;
  cursor: pointer;
  transition: background 0.15s ease;
}

.btn.primary {
  background: var(--primary);
  color: #fff;
}

.btn.primary:hover:not(:disabled) {
  background: var(--primary-dark);
}

.btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.layout {
  flex: 1 1 auto;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) minmax(380px, 0.9fr);
  gap: 16px;
  padding: 16px;
}

.editor-pane {
  position: relative;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 12px;
  overflow: hidden;
  min-height: 0;
}

.side-pane {
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-right: 2px;
}

.card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px 16px;
}

.card-title {
  font-size: 13px;
  font-weight: 500;
  color: var(--text);
  margin-bottom: 8px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.tag {
  font-size: 11px;
  font-weight: 400;
  padding: 2px 8px;
  border-radius: 999px;
}

.tag-ai {
  background: #e8f0fe;
  color: var(--primary-dark);
}

.tag-rule {
  background: #f1f3f6;
  color: var(--text-2);
}

.tag-verified {
  background: var(--green-bg);
  color: var(--green);
}

.tag-unverified {
  background: var(--amber-bg);
  color: var(--amber);
}

.tag-triage {
  background: #eef2ff;
  color: #4338ca;
}

.tag-review-pass {
  background: var(--green-bg);
  color: var(--green);
}

.tag-review-warn {
  background: var(--amber-bg);
  color: var(--amber);
}

.verify-note {
  margin: 8px 0 0;
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  background: var(--green-bg);
  color: var(--green);
  border-radius: 8px;
  padding: 8px 10px;
}

.diagnosis .block.review-block {
  padding: 10px 12px;
  background: #fffdf5;
  border-radius: 8px;
  margin-top: 8px;
}

.review-list {
  margin: 0;
  padding-left: 18px;
  line-height: 1.7;
  font-size: 13px;
}

.review-hint {
  margin: 8px 0 0;
  font-size: 12.5px;
  color: var(--amber);
  line-height: 1.6;
}

.round-track {
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.round-item {
  display: flex;
  align-items: baseline;
  gap: 7px;
  font-size: 12.5px;
  line-height: 1.55;
}

.round-name {
  flex-shrink: 0;
  color: var(--text-2);
}

.round-err {
  color: var(--text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.status {
  border-left: 4px solid #c2c8d0;
}

.status.running {
  border-left-color: var(--primary);
  background: #f6f9ff;
}

.status.ok {
  border-left-color: var(--green);
  background: var(--green-bg);
}

.status.err {
  border-left-color: var(--red);
  background: var(--red-bg);
}

.status-title {
  font-size: 14px;
  font-weight: 500;
}

.status-detail {
  margin-top: 4px;
  font-size: 12.5px;
  color: var(--text-2);
  font-family: var(--mono);
  word-break: break-all;
}

.terminal {
  margin: 0;
  font-family: var(--mono);
  font-size: 12.5px;
  line-height: 1.55;
  background: #f7f8fa;
  border-radius: 8px;
  padding: 10px 12px;
  overflow: auto;
  max-height: 220px;
  white-space: pre-wrap;
  word-break: break-all;
  color: #24292f;
}

.terminal.error-text {
  background: #fbf5f5;
  color: #6b2020;
}

.terminal.fixed {
  background: #f4faf6;
  color: #17342a;
}

.muted {
  color: var(--text-3);
  margin: 0;
  font-size: 13px;
}

.skeleton {
  height: 12px;
  border-radius: 6px;
  background: #eceef2;
  margin: 10px 0;
  animation: pulse 1.2s ease-in-out infinite;
}

.skeleton.short {
  width: 72%;
}

.skeleton.shorter {
  width: 48%;
}

@keyframes pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.45;
  }
}

.diagnosis .block {
  padding: 10px 0;
  border-top: 1px solid #f0f2f5;
}

.diagnosis .block:first-of-type {
  border-top: none;
  padding-top: 2px;
}

.block-title {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-3);
  margin-bottom: 6px;
}

.diagnosis p {
  margin: 0;
  line-height: 1.65;
}

.fix-list {
  margin: 0;
  padding-left: 20px;
  line-height: 1.7;
}

.apply {
  margin-top: 10px;
  width: 100%;
}

.notice {
  margin: 10px 0 0;
  font-size: 12.5px;
  line-height: 1.6;
  background: var(--amber-bg);
  color: var(--amber);
  border-radius: 8px;
  padding: 8px 10px;
}

.notice.warn {
  background: var(--red-bg);
  color: var(--red);
}

.welcome h2 {
  margin: 0 0 8px;
  font-size: 15px;
}

.welcome p {
  margin: 0 0 10px;
  line-height: 1.7;
  color: var(--text-2);
}

.feature-list {
  margin: 0 0 10px;
  padding-left: 20px;
  line-height: 1.9;
}

.hint {
  font-size: 12.5px;
  color: var(--text-3);
}

.tag-case {
  background: #f3ecff;
  color: #6a48c8;
}

/* ---- 流式诊断 ---- */

.tag-stream {
  background: #eef4ff;
  color: var(--primary-dark);
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.pulse-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--primary);
  flex: 0 0 auto;
  animation: pulse-dot 1.1s ease-in-out infinite;
}

@keyframes pulse-dot {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.3;
    transform: scale(0.75);
  }
}

.tag-round {
  background: #f1f3f6;
  color: var(--text-2);
}

.round-log {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 2px 0 10px;
}

.round-item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-3);
}

.round-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex: 0 0 auto;
}

.round-item.pass .round-dot {
  background: var(--green);
}

.round-item.fail .round-dot {
  background: var(--amber);
}

.caret {
  display: inline-block;
  width: 7px;
  height: 1em;
  margin-left: 2px;
  vertical-align: text-bottom;
  background: var(--primary);
  opacity: 0.85;
  animation: caret-blink 0.9s step-end infinite;
}

@keyframes caret-blink {
  50% {
    opacity: 0;
  }
}

.btn.ghost {
  background: #fff;
  border: 1px solid var(--border);
  color: var(--text);
  padding: 7px 14px;
  font-size: 13px;
}

.btn.ghost:hover:not(:disabled) {
  border-color: var(--primary);
  color: var(--primary-dark);
}

.kb-count {
  display: inline-block;
  margin-left: 6px;
  min-width: 17px;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--primary);
  color: #fff;
  font-size: 11px;
  line-height: 17px;
}

.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(21, 26, 38, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}

.modal {
  width: min(560px, calc(100vw - 40px));
  max-height: min(640px, calc(100vh - 80px));
  overflow-y: auto;
  background: #fff;
  border-radius: 14px;
  padding: 20px 22px 22px;
}

.modal-head {
  position: relative;
  margin-bottom: 14px;
}

.modal-head h2 {
  margin: 0 0 4px;
  font-size: 16px;
}

.modal-head p {
  margin: 0;
  font-size: 12.5px;
  color: var(--text-3);
  line-height: 1.6;
  padding-right: 34px;
}

.modal-close {
  position: absolute;
  top: 0;
  right: 0;
  width: 28px;
  height: 28px;
  border: none;
  background: #f1f3f6;
  border-radius: 8px;
  cursor: pointer;
  color: var(--text-2);
  font-size: 13px;
}

.modal-close:hover {
  background: #e6e9ee;
}

.upload-zone {
  display: block;
  border: 1.5px dashed var(--border);
  border-radius: 12px;
  padding: 24px 16px;
  text-align: center;
  cursor: pointer;
  transition: border-color 0.15s ease, background 0.15s ease;
}

.upload-zone:hover,
.upload-zone.dragging {
  border-color: var(--primary);
  background: #f6f9ff;
}

.upload-zone.busy {
  cursor: wait;
  opacity: 0.75;
}

.uz-title {
  font-size: 13.5px;
  font-weight: 500;
}

.uz-sub {
  margin-top: 5px;
  font-size: 12px;
  color: var(--text-3);
}

.kb-msg {
  margin: 10px 0 0;
  font-size: 12.5px;
  border-radius: 8px;
  padding: 8px 10px;
  line-height: 1.6;
}

.kb-msg.err {
  background: var(--red-bg);
  color: var(--red);
}

.kb-msg.ok {
  background: var(--green-bg);
  color: var(--green);
}

.doc-list {
  margin-top: 16px;
}

.doc-list-title {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-3);
  margin-bottom: 6px;
}

.doc-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 2px;
  border-top: 1px solid #f0f2f5;
}

.doc-main {
  min-width: 0;
}

.doc-name {
  font-size: 13.5px;
  word-break: break-all;
}

.doc-meta {
  margin-top: 2px;
  font-size: 12px;
  color: var(--text-3);
}

.doc-del {
  flex: 0 0 auto;
  font-size: 12.5px;
  padding: 6px 12px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: #fff;
  color: var(--text-2);
  cursor: pointer;
  transition: all 0.15s ease;
}

.doc-del:hover:not(:disabled) {
  border-color: var(--red);
  color: var(--red);
}

.doc-del.confirm {
  background: var(--red);
  border-color: var(--red);
  color: #fff;
}

.doc-del:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

/* ---- 多轮追问 ---- */

.chat-card {
  padding-bottom: 12px;
}

.chat-sub {
  font-size: 11.5px;
  font-weight: 400;
  color: var(--text-3);
}

.chat-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 440px;
  overflow-y: auto;
  padding: 2px 2px 4px;
}

.chat-empty p {
  margin: 0 0 10px;
  font-size: 12.5px;
  color: var(--text-3);
  line-height: 1.7;
}

.chat-suggestions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.chip {
  font-size: 12px;
  padding: 4px 10px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: #fff;
  color: var(--text-2);
  cursor: pointer;
  transition: all 0.15s ease;
}

.chip:hover {
  border-color: var(--primary);
  color: var(--primary-dark);
}

.chat-msg {
  display: flex;
}

.chat-msg.user {
  justify-content: flex-end;
}

.bubble {
  max-width: 92%;
  border-radius: 12px;
  padding: 8px 12px;
  font-size: 13px;
  line-height: 1.65;
  min-width: 0;
}

.chat-msg.user .bubble {
  background: #eaf1fe;
  border-bottom-right-radius: 4px;
}

.chat-msg.assistant .bubble {
  background: #f7f8fa;
  border-bottom-left-radius: 4px;
}

.bubble-failed {
  background: var(--red-bg) !important;
}

.chat-status {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--primary-dark);
  margin-bottom: 4px;
}

.chat-text {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
}

.alt-card {
  margin-top: 8px;
  border-radius: 10px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  background: #fff;
}

.alt-ok {
  border-color: #cdebd8;
}

.alt-fail {
  border-color: #f3ddc0;
}

.alt-head {
  font-size: 13px;
  font-weight: 500;
  margin-bottom: 6px;
}

.alt-ok .alt-head {
  color: var(--green);
}

.alt-fail .alt-head {
  color: var(--amber);
}

.alt-line {
  font-size: 12.5px;
  color: var(--text-2);
  margin-bottom: 8px;
  line-height: 1.6;
}

.alt-card .terminal {
  max-height: 180px;
  margin-bottom: 8px;
}

.alt-card .verify-note {
  margin-bottom: 8px;
}

.alt-attempts {
  margin-bottom: 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.alt-attempt {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-3);
  word-break: break-word;
}

.dot-ok {
  background: var(--green);
}

.dot-fail {
  background: var(--amber);
}

.alt-section {
  margin-top: 8px;
}

.alt-section-title {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-3);
  margin-bottom: 4px;
}

.alt-section ul {
  margin: 0;
  padding-left: 18px;
  line-height: 1.7;
  font-size: 13px;
}

.alt-suggestion {
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
}

.chip-btn {
  font-size: 12.5px;
  padding: 6px 12px;
}

.chat-input-row {
  display: flex;
  gap: 8px;
  margin-top: 10px;
  align-items: flex-end;
}

.chat-input {
  flex: 1 1 auto;
  resize: none;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 8px 10px;
  font-size: 13px;
  font-family: inherit;
  line-height: 1.5;
  outline: none;
  max-height: 96px;
  min-height: 36px;
  background: #fff;
  color: var(--text);
}

.chat-input:focus {
  border-color: var(--primary);
}

.chat-input:disabled {
  opacity: 0.6;
}

.chat-send {
  flex: 0 0 auto;
  padding: 8px 16px;
}
</style>
