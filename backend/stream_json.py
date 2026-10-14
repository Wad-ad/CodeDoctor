"""流式 JSON 增量解析:从 LLM 的 JSON token 流里「边生成边抽取」目标字段。

背景:诊断结果是结构化 JSON(explain / reason / fix_points / fixed_code),
但用户希望看到打字机式的实时内容。直接把原始 JSON 吐给前端又丑又没意义,
所以这里做一层增量解析 —— 在 JSON 还没生成完的时候,就实时抽出
面向用户的字段,产出细粒度事件:

    {"type": "field", "field": "explain", "phase": "start"}
    {"type": "delta", "field": "explain", "text": "程序在计算…"}
    {"type": "item",  "field": "fix_points", "text": "第 4 行:…"}
    {"type": "field", "field": "explain", "phase": "end"}

设计要点:
- 永不抛异常:任何无法理解的输入都静默停发事件(最终由完整 JSON 解析兜底);
- 任意切分安全:逐字符状态机 + 转义 / unicode / surrogate pair 处理,分片边界无影响;
- 顺序无关:字段按模型实际生成顺序处理,先到先发。

非目标结构(其他字段、嵌套对象等)统一跳过,不影响目标字段的提取。
"""

_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", '"': '"',
    "\\": "\\", "/": "/", "b": "\b", "f": "\f",
}

_FLUSH_LEN = 8  # 目标字符串字段攒够这么多字符发一批 delta(降频,又不影响观感)


class _StrSink:
    """处理 JSON 字符串内部:转义、\\uXXXX、surrogate pair。

    feed(ch) 返回 True 表示遇到了未转义的结束引号(字符串到此闭合)。
    解码后的可见字符通过 put 回调输出。
    """

    __slots__ = ("put", "mode", "uni", "high")  # mode: 0=普通 1=转义 2=unicode

    def __init__(self, put):
        self.put = put
        self.mode = 0
        self.uni = ""
        self.high = None

    def feed(self, ch: str) -> bool:
        mode = self.mode
        if mode == 2:
            self.uni += ch
            if len(self.uni) == 4:
                self._emit_uni()
                self.mode = 0
            return False
        if mode == 1:
            self.mode = 0
            if ch == "u":
                self.mode = 2
                self.uni = ""
            else:
                self.put(_ESCAPES.get(ch, ch))
            return False
        if ch == "\\":
            self.mode = 1
            return False
        if ch == '"':
            return True
        self.put(ch)
        return False

    def _emit_uni(self):
        try:
            v = int(self.uni, 16)
        except ValueError:
            self.high = None
            self.put("?")
            return
        if 0xD800 <= v <= 0xDBFF:          # 高代理:暂存,等低位组合
            self.high = v
            return
        if 0xDC00 <= v <= 0xDFFF and self.high is not None:
            self.put(chr(0x10000 + ((self.high - 0xD800) << 10) + (v - 0xDC00)))
            self.high = None
            return
        self.high = None
        self.put(chr(v))


class StreamFieldExtractor:
    """从流式 JSON 文本中增量提取目标字段,产出事件列表。"""

    def __init__(self, fields=("explain", "reason", "fix_points", "fixed_code")):
        self.fields = set(fields)
        self.broken = False   # 输入无法理解:静默停发(交给完整解析兜底)
        self.done = False     # 顶层对象已闭合
        self._state = "seek"
        self._out = None
        self._reset_scan()

    # ------------------------------------------------------------ 对外接口

    def feed(self, text: str) -> list:
        """喂入一段增量文本,返回这段时间产生的事件(可能为空)。"""
        if self.broken or self.done or not text:
            return []
        self._out = []
        try:
            for ch in text:
                self._step(ch)
                if self.done or self.broken:
                    break
        except Exception:
            self.broken = True
        out = self._out
        self._out = None
        return out

    # ------------------------------------------------------------ 状态复位

    def _reset_scan(self):
        self._key_buf = []
        self._key_sink = None
        self._key = ""
        self._cur = None          # 当前目标字段名
        self._pending = ""        # 待发 delta 缓冲
        self._str_sink = None
        self._item_buf = []
        self._item_sink = None
        self._depth = 0           # 跳过嵌套结构时的括号深度
        self._skip_in_str = False
        self._skip_esc = False

    # ------------------------------------------------------------ 状态机

    def _step(self, ch: str):
        state = self._state

        if state == "seek":                       # 找顶层对象的 '{'
            if ch == "{":
                self._state = "key_open"

        elif state == "key_open":                 # 等待一个字段名或对象结束
            if ch == '"':
                self._key_buf = []
                self._key_sink = _StrSink(self._key_buf.append)
                self._state = "key_str"
            elif ch == "}":
                self._done = True

        elif state == "key_str":                  # 字段名(转义按原样收集,反正不匹配)
            if self._key_sink.feed(ch):
                self._key = "".join(self._key_buf)
                self._state = "colon"

        elif state == "colon":
            if ch == ":":
                self._state = "value"

        elif state == "value":                    # 判断值类型
            if ch == '"':
                if self._key in self.fields:
                    self._cur = self._key
                    self._pending = ""
                    self._str_sink = _StrSink(self._pend_put)
                    self._emit({"type": "field", "field": self._cur, "phase": "start"})
                    self._state = "str"
                else:
                    self._state = "skip_str"
            elif ch == "[":
                if self._key in self.fields:
                    self._cur = self._key
                    self._emit({"type": "field", "field": self._cur, "phase": "start"})
                    self._state = "arr"
                else:
                    self._depth = 1
                    self._skip_in_str = False
                    self._skip_esc = False
                    self._state = "skip_any"
            elif ch == "{":
                self._depth = 1
                self._skip_in_str = False
                self._skip_esc = False
                self._state = "skip_any"
            elif ch in " \t\r\n":
                pass
            else:                                  # 数字 / true / false / null
                self._state = "skip_scalar"

        elif state == "skip_scalar":               # 跳过非字符串标量
            if ch == ",":
                self._state = "key_open"
            elif ch == "}":
                self._done = True

        elif state == "skip_str":                  # 跳过非目标字符串
            if self._skip_esc:
                self._skip_esc = False
            elif ch == "\\":
                self._skip_esc = True
            elif ch == '"':
                self._state = "after_value"

        elif state == "skip_any":                  # 跳过嵌套对象 / 数组
            if self._skip_esc:
                self._skip_esc = False
            elif self._skip_in_str:
                if ch == "\\":
                    self._skip_esc = True
                elif ch == '"':
                    self._skip_in_str = False
            else:
                if ch == '"':
                    self._skip_in_str = True
                elif ch in "[{":
                    self._depth += 1
                elif ch in "]}":
                    self._depth -= 1
                    if self._depth <= 0:
                        self._state = "after_value"

        elif state == "after_value":               # 值结束,等待 , 或 }
            if ch == ",":
                self._state = "key_open"
            elif ch == "}":
                self._done = True

        elif state == "str":                       # 目标字符串字段内部(实时抽取)
            if self._str_sink.feed(ch):
                self._flush_pending()
                self._emit({"type": "field", "field": self._cur, "phase": "end"})
                self._cur = None
                self._state = "after_value"

        elif state == "arr":                       # 目标数组内部(fix_points)
            if ch == '"':
                self._item_buf = []
                self._item_sink = _StrSink(self._item_buf.append)
                self._state = "arr_item"
            elif ch == "]":
                self._emit({"type": "field", "field": self._cur, "phase": "end"})
                self._cur = None
                self._state = "after_value"

        elif state == "arr_item":                  # 数组元素字符串
            if self._item_sink.feed(ch):
                text = "".join(self._item_buf).strip()
                if text:
                    self._emit({"type": "item", "field": self._cur, "text": text})
                self._state = "arr"

    # ------------------------------------------------------------ 输出小工具

    def _emit(self, event: dict):
        self._out.append(event)

    def _pend_put(self, s: str):
        self._pending += s
        if len(self._pending) >= _FLUSH_LEN:
            self._flush_pending()

    def _flush_pending(self):
        if self._pending and self._cur:
            self._emit({"type": "delta", "field": self._cur, "text": self._pending})
            self._pending = ""
