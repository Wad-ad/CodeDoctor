# -*- coding: utf-8 -*-
"""探针:确认 json dumps/loads 对代理对的处理;以及测试文件里那一行的真实内容。"""
import json

BS = chr(92)
s_surrogate = chr(0xD83D) + chr(0xDE00)   # 裸代理对(2 码点)
s_emoji = chr(0x1F600)                     # 真 emoji(1 码点)

print("surrogate==emoji:", s_surrogate == s_emoji)
print("len(surrogate):", len(s_surrogate))

js = json.dumps({"r": s_surrogate}, ensure_ascii=True)
js2 = json.dumps({"r": s_emoji}, ensure_ascii=True)
print("dumps surrogate:", repr(js))
print("dumps emoji     :", repr(js2))

r1 = json.loads(js)["r"]
print("loads surrogate == emoji:", r1 == s_emoji, "len:", len(r1))
print("loads surrogate repr:", repr(r1))

# 读测试文件里 reason 那一行的真实内容
p = "test_stream_json.py"
data = open(p, encoding="utf-8").read()
for i, line in enumerate(data.splitlines(), 1):
    if '"reason"' in line:
        print(f"file line {i}:", repr(line))
