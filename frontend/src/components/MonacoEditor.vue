<template>
  <div ref="host" class="monaco-host"></div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import * as monaco from 'monaco-editor'
import EditorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker'

self.MonacoEnvironment = {
  getWorker: () => new EditorWorker(),
}

const props = defineProps({
  initialValue: {
    type: String,
    default: '# 把你的代码粘贴到这里\nprint("Hello, CodeDoctor!")\n',
  },
})

const host = ref(null)
let editor = null
let decorations = null

onMounted(() => {
  editor = monaco.editor.create(host.value, {
    value: props.initialValue,
    language: 'python',
    theme: 'vs',
    fontSize: 14,
    lineHeight: 22,
    fontFamily: "'Cascadia Code', Consolas, 'Courier New', monospace",
    minimap: { enabled: false },
    glyphMargin: true,
    lineNumbersMinChars: 3,
    scrollBeyondLastLine: false,
    automaticLayout: true,
    tabSize: 4,
    insertSpaces: true,
    renderLineHighlight: 'line',
    padding: { top: 12, bottom: 12 },
    scrollbar: { verticalScrollbarSize: 10, horizontalScrollbarSize: 10 },
    wordWrap: 'off',
  })
})

onBeforeUnmount(() => {
  editor?.dispose()
  editor = null
  decorations = null
})

function getValue() {
  return editor ? editor.getValue() : ''
}

/** 把代码填回编辑器,保留撤销栈(Ctrl+Z 可以退回) */
function setCode(code) {
  if (!editor) return
  const model = editor.getModel()
  editor.executeEdits('codedoctor-fix', [
    { range: model.getFullModelRange(), text: code },
  ])
  editor.pushUndoStop()
  editor.setPosition({ lineNumber: 1, column: 1 })
}

/** 给指定行加「报错」标记:整行淡红底 + 行号旁红点 + 红色波浪线 */
function markErrorLine(line) {
  if (!editor || !line) return
  const model = editor.getModel()
  const target = Math.min(Math.max(Number(line), 1), model.getLineCount())
  const range = new monaco.Range(target, 1, target, model.getLineMaxColumn(target))
  const options = {
    isWholeLine: true,
    className: 'cd-error-line',
    glyphMarginClassName: 'cd-error-glyph',
    inlineClassName: 'cd-error-squiggly',
    overviewRuler: {
      color: '#e24b4a',
      position: monaco.editor.OverviewRulerLane.Right,
    },
  }
  if (!decorations) {
    decorations = editor.createDecorationsCollection([{ range, options }])
  } else {
    decorations.set([{ range, options }])
  }
  editor.revealLineInCenter(target)
}

function clearErrorMark() {
  decorations?.clear()
}

defineExpose({ getValue, setCode, markErrorLine, clearErrorMark })
</script>

<style scoped>
.monaco-host {
  position: absolute;
  inset: 0;
}
</style>
