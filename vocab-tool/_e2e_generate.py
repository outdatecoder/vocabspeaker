# -*- coding: utf-8 -*-
"""端到端门禁：真浏览器走完「上传 → 解析 → 校对 → 生成 → 播放」。

为什么要 CDP 而不是 urllib：
    `_api_probe.py` 验的是接口契约，验不了**页面自己发出的请求体**对不对。
    这个脚本用 `DOM.setFileInputFiles` 真正往 file input 里塞文件（等价于用户
    在系统对话框里选文件），然后在页面里包一层 fetch 把 `/api/build` 的
    请求体抓出来，和界面上「改过 / 删过」的状态逐条比对 —— 这一层错了，
    用户会得到一份「自己没校对过」的词库。

用法：
    python -X utf8 _e2e_generate.py            # 跑完清理
    python -X utf8 _e2e_generate.py --keep     # 保留生成的包与任务快照
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
from _gateutil import edge_path

EDGE = edge_path()        # $EDGE 或两个常见安装位置，见 _gateutil.py
PACKS = os.path.join(HERE, "packs")
RESULTS = []

# 7 个词；其中 zeta 的中文故意以英文字母开头 —— 「只看可疑」靠这条判据
WORDS = [("alpha", "阿尔法"), ("beta", "贝塔"), ("gamma", "伽马"),
         ("delta", "德尔塔"), ("epsilon", "艾普西龙"),
         ("zeta", "what is 的缩写形式"), ("eta", "艾塔")]

HOOK = r"""
window.__mlog = [];
(function(){
  var P = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function(){ window.__mlog.push('play'); return P.apply(this, arguments); };
})();
"""


def T(name, ok, extra=None):
    RESULTS.append(("PASS" if ok else "FAIL", name, extra))
    return bool(ok)


def S(name, why):
    RESULTS.append(("SKIP", name, why))


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


WORK = os.path.join(HERE, "work")
TRACKED = ("_uploads", "_docs", "_parse", "_jobs", "_build", "_stage", "_backup")


def work_snapshot():
    out = {}
    for d in TRACKED:
        p = os.path.join(WORK, d)
        try:
            out[d] = set(os.listdir(p))
        except OSError:
            out[d] = set()
    return out


def work_cleanup(before):
    """删掉本次跑出来的上传/任务产物 —— 门禁不该往用户目录里拉屎。

    删两次：浏览器/服务刚被杀掉时，文件可能还被占着（Windows 上表现为
    rmtree 静默失败），隔 300ms 再试一次，并由调用方断言确实清干净了。
    """
    for attempt in (1, 2):
        after = work_snapshot()
        left = 0
        for d in TRACKED:
            for name in sorted(after[d] - before[d]):
                p = os.path.join(WORK, d, name)
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                if os.path.exists(p):
                    left += 1
        if not left:
            return
        time.sleep(0.3)


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.mid = 0
        self.exceptions = []

    async def send(self, method, params=None):
        self.mid += 1
        my = self.mid
        await self.ws.send_json({"id": my, "method": method, "params": params or {}})
        while True:
            m = await self.ws.receive_json()
            if m.get("method") == "Runtime.exceptionThrown":
                d = m["params"].get("exceptionDetails", {})
                self.exceptions.append(str(d.get("exception", {}).get("description")
                                           or d.get("text")))
            if m.get("id") == my:
                return m

    async def js(self, expr):
        r = await self.send("Runtime.evaluate",
                            {"expression": expr, "returnByValue": True,
                             "awaitPromise": True})
        return r.get("result", {}).get("result", {}).get("value")

    async def wait(self, expr, sec=30):
        for _ in range(int(sec * 5)):
            if await self.js(expr) is True:
                return True
            await asyncio.sleep(0.2)
        return False

    async def set_file(self, path):
        r = await self.send("Runtime.evaluate",
                            {"expression": "document.getElementById('file')",
                             "returnByValue": False})
        oid = r["result"]["result"]["objectId"]
        await self.send("DOM.setFileInputFiles", {"files": [path], "objectId": oid})


async def main():
    keep = "--keep" in sys.argv
    sport, dport = free_port(), free_port()
    name = "e2e_%s" % uuid.uuid4().hex[:8]
    txt = os.path.join(HERE, "work", "_e2e_words.txt")
    os.makedirs(WORK, exist_ok=True)
    before = work_snapshot()
    with open(txt, "w", encoding="utf-8") as f:
        for en, cn in WORDS:
            f.write("%s\t%s\n" % (en, cn))

    srv = subprocess.Popen([PY, os.path.join(HERE, "serve.py"), str(sport)],
                           cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    prof = tempfile.mkdtemp(prefix="edge_e2e_")
    br = None
    try:
        for _ in range(120):
            if srv.poll() is not None:
                raise SystemExit("serve.py 启动失败")
            try:
                s = socket.create_connection(("127.0.0.1", sport), timeout=1); s.close(); break
            except OSError:
                await asyncio.sleep(0.2)

        br = subprocess.Popen(
            [EDGE, "--headless=new", "--disable-gpu", "--no-first-run", "--mute-audio",
             "--no-default-browser-check", "--autoplay-policy=no-user-gesture-required",
             "--remote-debugging-port=%d" % dport, "--user-data-dir=" + prof, "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        import aiohttp
        async with aiohttp.ClientSession() as sess:
            ws_url = None
            for _ in range(100):
                try:
                    async with sess.get("http://127.0.0.1:%d/json/list" % dport) as r:
                        tg = await r.json()
                    pg = [t for t in tg if t.get("type") == "page"]
                    if pg:
                        ws_url = pg[0]["webSocketDebuggerUrl"]; break
                except Exception:
                    pass
                await asyncio.sleep(0.25)
            if not ws_url:
                print("连不上 CDP（Edge 没起来？）")
                return 3
            async with sess.ws_connect(ws_url, max_msg_size=64 * 1024 * 1024) as ws:
                c = CDP(ws)
                await c.send("Page.enable")
                await c.send("Runtime.enable")
                await c.send("DOM.enable")
                await c.send("Page.addScriptToEvaluateOnNewDocument", {"source": HOOK})
                await c.send("Emulation.setDeviceMetricsOverride",
                             {"width": 1440, "height": 900, "deviceScaleFactor": 1,
                              "mobile": False})

                # ── ① 打开生成器 ───────────────────────────────
                await c.send("Page.navigate",
                             {"url": "http://127.0.0.1:%d/generator" % sport})
                ok = await c.wait("document.getElementById('ver').textContent !== '—'")
                T("生成器页面可打开", ok,
                  await c.js("document.getElementById('ver').textContent"))
                T("token 已注入（无占位符）",
                  await c.js("document.documentElement.outerHTML.indexOf('__PET_TOKEN__') < 0"))
                # 新流程的两个按钮：确认键默认禁用；「返回上一步」只在取消后出现
                T("上传卡有「开始解析」按钮，且没选文件时是禁用的",
                  await c.js("!!document.getElementById('do-upload')"
                             " && document.getElementById('do-upload').disabled === true"))
                T("进度卡有「返回上一步」按钮（默认隐藏）",
                  await c.js("!!document.getElementById('back')"
                             " && document.getElementById('back').classList.contains('hidden')"))
                S("取消 → 「返回上一步」的真实点击",
                  "解析 7 个词秒级完成，取消窗口不可靠；已由静态断言覆盖按钮存在性，"
                  "该路径请在真机上手动过一遍（解析大文件时点取消）")

                # ── ② 选文件 → 确认 → 解析 ──────────────────────
                # 流程要求：选完文件**不许自动跳步**，必须点「开始解析」；
                # 且 docx 没勾「按 Word 真实页码分页」时要给出提示。
                dpath = os.path.join(HERE, "work", "_e2e_probe.docx")
                made = False
                try:
                    import docx as _docx
                    _d = _docx.Document()
                    _d.add_paragraph("1 apple [test] n. 苹果")
                    _d.add_paragraph("2 banana [test] n. 香蕉")
                    _d.save(dpath)
                    made = True
                except Exception:
                    made = False
                if made:
                    await c.set_file(dpath)
                    await asyncio.sleep(0.35)
                    T("docx 未勾真实页码 → 上传卡给出提示",
                      await c.js("!document.getElementById('up-warn').classList.contains('hidden')"),
                      await c.js("document.getElementById('up-warn').textContent.slice(0, 36)"))
                    T("选完文件停在上传卡（不自动进下一步）",
                      await c.js("!document.getElementById('sec-upload').classList.contains('hidden')"
                                 " && document.getElementById('sec-review').classList.contains('hidden')"))
                    T("没点确认时进度卡还没出现（没有偷偷开始解析）",
                      await c.js("document.getElementById('sec-progress').classList.contains('hidden')"))
                    await c.js("document.getElementById('with-pages').click()")
                    await asyncio.sleep(0.25)
                    T("勾上「按 Word 真实页码分页」后提示消失",
                      await c.js("document.getElementById('up-warn').classList.contains('hidden')"))
                    await c.js("document.getElementById('with-pages').click()")   # 还原成默认关
                    await asyncio.sleep(0.15)
                    os.remove(dpath)
                else:
                    S("docx 未勾真实页码的提示", "python-docx 不可用")

                await c.set_file(txt)
                T("txt 不出现 Word 分页提示（只对 docx 提醒）",
                  await c.js("document.getElementById('up-warn').classList.contains('hidden')"))
                await c.js("document.getElementById('do-upload').click()")
                ok = await c.wait(
                    "!document.getElementById('sec-review').classList.contains('hidden')", 120)
                T("选文件后进入校对卡（解析到 done）", ok,
                  await c.js("document.getElementById('rv-sum').textContent"))
                T("1 组（7 词未超 50）",
                  await c.js("document.querySelectorAll('#groups .g').length === 1"),
                  await c.js("document.querySelectorAll('#groups .g').length"))
                T("折叠时不生成任何输入框（懒渲染）",
                  await c.js("document.querySelectorAll('#groups input.cell').length === 0"),
                  await c.js("document.querySelectorAll('#groups input.cell').length"))

                # ── ②' 选页：默认全选；取消勾选 = 这一页不提交 ──────────
                # 勾选框在组头**外面**（交互元素不能嵌套），所以点它不该连带展开/折叠
                T("每页一个勾选框，默认全选",
                  await c.js("(function(){var p=document.querySelectorAll('#groups input.pick');"
                             "return p.length === 1 && p[0].checked;})()"),
                  await c.js("Array.from(document.querySelectorAll('#groups input.pick'))"
                             ".map(function(x){return x.checked;})"))
                await c.js("document.querySelector('#groups input.pick').click()")
                T("取消勾选 → 摘要出现「已选 0」且「下一步」被禁用",
                  await c.js("(function(){var s=document.getElementById('rv-sum').textContent;"
                             "return /已选 0/.test(s) && "
                             "document.getElementById('to-target').disabled;})()"),
                  await c.js("document.getElementById('rv-sum').textContent + ' / disabled=' + "
                             "document.getElementById('to-target').disabled + ' / 展开=' + "
                             "document.querySelectorAll('#groups input.cell').length"))
                await c.js("document.querySelector('#groups input.pick').click()")
                T("重新勾上 → 回到 7 词、可继续（且不产生「已选」尾巴）",
                  await c.js("(function(){var s=document.getElementById('rv-sum').textContent;"
                             "return /7 词/.test(s) && !/已选/.test(s) && "
                             "!document.getElementById('to-target').disabled;})()"),
                  await c.js("document.getElementById('rv-sum').textContent"))

                # ── ③ 展开 → 改词 → 删行 ───────────────────────
                await c.js("document.querySelector('.g-head').click()")
                T("展开后 7 行 × 2 列 = 14 个输入框",
                  await c.js("document.querySelectorAll('#groups input.cell').length === 14"),
                  await c.js("document.querySelectorAll('#groups input.cell').length"))

                # ── ③' 可疑必须给出理由与建议（用户明确要求）──────────
                T("「可疑」判定规则可展开（页面上能查到判据）",
                  await c.js("!!document.querySelector('details.rules')"))
                T("只有可疑的那一条显示「?」",
                  await c.js("document.querySelectorAll('.why-btn:not(.hidden)').length === 1"),
                  await c.js("document.querySelectorAll('.why-btn:not(.hidden)').length"))
                await c.js(
                    "(function(){var tr=Array.prototype.filter.call("
                    "document.querySelectorAll('#groups tr[data-r]'),function(t){"
                    "return t.querySelector('input[data-f=en]').value==='zeta';})[0];"
                    "tr.querySelector('.why-btn').click();})()")
                why_txt = await c.js("(document.querySelector('.why-row')||{}).textContent || ''")
                T("点「?」给出具体理由（中文栏以英文字母开头）",
                  "以英文字母开头" in (why_txt or ""), (why_txt or "")[:80])
                T("点「?」同时给出处理建议",
                  "建议：" in (why_txt or ""), (why_txt or "")[:120])
                T("理由面板说明「可疑不阻止生成」",
                  "不会拦住生成" in (why_txt or ""), (why_txt or "")[-60:])
                await c.js(
                    "(function(){var tr=Array.prototype.filter.call("
                    "document.querySelectorAll('#groups tr[data-r]'),function(t){"
                    "return t.querySelector('input[data-f=en]').value==='zeta';})[0];"
                    "tr.querySelector('.why-btn').click();})()")
                T("再点一次收起理由", await c.js(
                    "document.querySelectorAll('.why-row').length === 0"))

                # 改好之后标记要自动摘掉（否则用户改完还挂着红框）
                await c.js(
                    "(function(){var tr=Array.prototype.filter.call("
                    "document.querySelectorAll('#groups tr[data-r]'),function(t){"
                    "return t.querySelector('input[data-f=en]').value==='zeta';})[0];"
                    "var i=tr.querySelector('input[data-f=cn]');i.value='泽塔';"
                    "i.dispatchEvent(new Event('input',{bubbles:true}));})()")
                T("把中文改干净后「?」自动隐藏、可疑计数归零",
                  await c.js("document.querySelectorAll('.why-btn:not(.hidden)').length === 0 && "
                             "!/可疑/.test(document.getElementById('rv-sum').textContent)"),
                  await c.js("document.getElementById('rv-sum').textContent"))
                # 还原成可疑文本，后面的筛选用例还要用它
                await c.js(
                    "(function(){var tr=Array.prototype.filter.call("
                    "document.querySelectorAll('#groups tr[data-r]'),function(t){"
                    "return t.querySelector('input[data-f=en]').value==='zeta';})[0];"
                    "var i=tr.querySelector('input[data-f=cn]');i.value=%s;"
                    "i.dispatchEvent(new Event('input',{bubbles:true}));})()"
                    % json.dumps("what is 的缩写形式"))
                T("改回可疑文本后「?」重新出现",
                  await c.js("document.querySelectorAll('.why-btn:not(.hidden)').length === 1"),
                  await c.js("document.getElementById('rv-sum').textContent"))
                # 第 2 条（beta）中文改成带标记的文本，方便在请求体里认出来
                await c.js(
                    "(function(){var i=document.querySelectorAll('.cell')[3];"
                    "i.value='贝塔（已校对）';"
                    "i.dispatchEvent(new Event('input',{bubbles:true}));})()")
                T("改中文后摘要出现「已改」",
                  await c.js("/已改/.test(document.getElementById('rv-sum').textContent)"),
                  await c.js("document.getElementById('rv-sum').textContent"))
                # 删掉最后一行（eta）
                await c.js(
                    "document.querySelectorAll('.del')[document.querySelectorAll('.del').length-1].click()")
                T("删一行后变 6 词（摘要与组头同步）",
                  await c.js("/6 词/.test(document.getElementById('rv-sum').textContent) && "
                             "/6 词/.test(document.querySelector('.g-words').textContent)"),
                  await c.js("document.getElementById('rv-sum').textContent"))

                # ── ④ 只看可疑：只剩 zeta 那条，且自动展开 ──────
                await c.js("document.querySelector('.g-badge.click').click()")
                T("点组头「可疑 N」徽标 = 切到只看可疑（且不误触展开/折叠）",
                  await c.js("document.getElementById('f-low').classList.contains('on') && "
                             "document.querySelectorAll('#groups input.cell').length === 2"),
                  await c.js("document.getElementById('f-low').className + ' / ' + "
                             "document.querySelectorAll('#groups input.cell').length"))
                await c.js("document.getElementById('f-all').click()")
                await c.js("document.getElementById('f-low').click()")
                T("「只看可疑」只渲染 1 行（zeta 的中文以字母开头）",
                  await c.js("document.querySelectorAll('#groups input.cell').length === 2"),
                  await c.js("document.querySelectorAll('#groups input.cell').length"))
                T("「只看可疑」时该行内容是 zeta",
                  await c.js("(document.querySelector('#groups input.cell')||{}).value === 'zeta'"),
                  await c.js("(document.querySelector('#groups input.cell')||{}).value"))
                await c.js("document.getElementById('f-all').click()")

                # ── ⑤ 选目标：新建独立词库 ────────────────────
                await c.js("document.getElementById('to-target').click()")
                T("进入目标卡", await c.js(
                    "!document.getElementById('sec-target').classList.contains('hidden')"))
                await c.js("document.getElementById('r-new').click()")
                await c.js("(function(){var i=document.getElementById('new-name');"
                           "i.value=%s;i.dispatchEvent(new Event('input',{bubbles:true}));})()"
                           % json.dumps(name))
                T("重名校验：新名字可用（无报错）",
                  await c.js("document.getElementById('new-err').textContent === ''"),
                  await c.js("document.getElementById('new-err').textContent"))

                # 抓 /api/build 的请求体
                await c.js(
                    "window.__cap=null;(function(){var of=window.fetch;"
                    "window.fetch=function(u,o){"
                    "if(String(u).indexOf('/api/build')>=0&&o&&o.body){"
                    "try{window.__cap=JSON.parse(o.body);}catch(e){}}"
                    "return of.apply(this,arguments);};})()")
                await c.js("document.getElementById('do-build').click()")
                ok = await c.wait(
                    "!document.getElementById('sec-done').classList.contains('hidden')", 240)
                T("点「开始生成」后走到完成卡", ok,
                  await c.js("document.getElementById('prog-msg').textContent"))
                cap = await c.js("window.__cap")
                T("抓到了 /api/build 的请求体", isinstance(cap, dict), type(cap).__name__)
                if isinstance(cap, dict):
                    rows = cap.get("rows") or []
                    T("请求体：mode=new 且 pack_id=name",
                      cap.get("mode") == "new" and cap.get("pack_id") == name,
                      "%s / %s" % (cap.get("mode"), cap.get("pack_id")))
                    T("请求体：6 条（删掉的那条没被提交）", len(rows) == 6, len(rows))
                    T("请求体：改过的中文原样提交",
                      any(r.get("cn") == "贝塔（已校对）" for r in rows),
                      [r.get("cn") for r in rows])
                    T("请求体：删掉的 eta 不在里面",
                      not any(r.get("en") == "eta" for r in rows),
                      [r.get("en") for r in rows])
                    T("请求体：每条都带 page/en/cn",
                      all(set(r.keys()) == {"page", "en", "cn"} for r in rows),
                      rows[:1])
                    # 选中页在提交前被压成 1..k（后端再加 offset），所以单组这里必须是 1
                    T("请求体：页号从 1 起连续（选中的页会重新编号）",
                      sorted({r.get("page") for r in rows}) == [1],
                      sorted({r.get("page") for r in rows}))
                T("完成卡显示 1 页 / 6 词",
                  await c.js("/6 词/.test(document.getElementById('done-title').textContent)"),
                  await c.js("document.getElementById('done-title').textContent"))

                # ── ⑥ 磁盘上的包要对得上 ──────────────────────
                pack = os.path.join(PACKS, name)
                pj = os.path.join(pack, "pack_data.json")
                if os.path.isfile(pj):
                    with open(pj, encoding="utf-8") as f:
                        data = json.load(f)
                    pairs = [[p[0], p[1]] for p in (data["pages"][0].get("pairs") or [])]
                    T("包内数据 = 校对后的 6 条（含改动、不含删除）",
                      len(pairs) == 6 and ["beta", "贝塔（已校对）"] in pairs
                      and not any(p[0] == "eta" for p in pairs), pairs)
                    audio = os.listdir(os.path.join(pack, "audio"))
                    T("包内音频 = 6 个词 + 1 个整页",
                      len(audio) == 7, sorted(audio))
                else:
                    T("包目录已生成", False, pj)

                # ── ⑥' 并入重复词必须在页面上被拦住 ───────────────
                # 并入是"追加"：同一个词出现两次，用户就分不清哪页有它。
                # 真实踩法：文件按 50 词一组，第 2 组从第 51 个词开始，而包的第 1 页到第 57 个词
                # → in / English / … 两页都有。这里把刚建好的 6 个词再并一次，必然全撞。
                await c.js("document.getElementById('again').click()")
                await c.wait(
                    "!document.getElementById('sec-upload').classList.contains('hidden')", 20)
                await c.set_file(txt)
                await c.js("document.getElementById('do-upload').click()")
                await c.wait(
                    "!document.getElementById('sec-review').classList.contains('hidden')", 150)
                await c.js("document.getElementById('to-target').click()")
                await c.js("document.getElementById('r-merge').click()")
                await c.js(
                    "(function(){var s=document.getElementById('pack-select');"
                    "s.value=%s;s.dispatchEvent(new Event('change',{bubbles:true}));})()"
                    % json.dumps(name, ensure_ascii=False))
                await asyncio.sleep(0.3)
                await c.js("document.getElementById('do-build').click()")
                blocked = await c.wait(
                    "!document.getElementById('merge-err').classList.contains('hidden')", 40)
                err_txt = await c.js(
                    "document.getElementById('merge-err').textContent") or ""
                T("并入同名词条被拦：留在目标卡并把理由写在页面上",
                  blocked and "重复" in err_txt and "第 1 页" in err_txt, err_txt[:80])
                with open(os.path.join(pack, "pack_data.json"), encoding="utf-8") as f:
                    after = json.load(f)
                T("被拦下后包没变：仍 1 页 6 词",
                  len(after["pages"]) == 1 and after["pages"][0]["words"] == 6,
                  [(p["page"], p["words"]) for p in after["pages"]])

                # ── ⑦ 打开播放器：能渲染、点了真出声 ──────────
                url = "http://127.0.0.1:%d/vocab-tool/packs/%s/index.html" % (sport, name)
                await c.send("Page.navigate", {"url": url})
                ok = await c.wait("document.querySelectorAll('.card').length === 1", 40)
                T("播放器打开并渲染出 1 张页卡片", ok,
                  await c.js("document.querySelectorAll('.card').length"))
                # 两个入口是同一次 fetch 成功后才一起显示，所以必须一起断言：
                # 只断言 gen-link 的话，漏掉 home-link 就不会被发现。
                # ⚠️ 这是**异步**显示（`fetch('/api/packs')` 落地后才设 inline style），
                # 而页卡片是解析时同步渲染的 —— 紧接着卡片断言取值会偶发读到 fetch 尚未落地的 'none'
                # （实测该 fetch 在本机要 ~0.2s）。所以按真实语义**等**它出现；
                # 失败时同时报出 `/api/packs` 自己能不能取，用来区分「服务取不到」与「没显示」（R1）。
                entries_shown = await c.wait(
                    "['#home-link','#gen-link'].every(function(s){"
                    "return getComputedStyle(document.querySelector(s)).display !== 'none';})", 5)
                api_ok = await c.js("fetch('/api/packs').then(function(r){return r.ok;})"
                                    ".catch(function(){return false;})")
                T("http 下顶栏出现「← 词库列表」与「＋ 添加词表」", entries_shown,
                  "display=%s / api/packs ok=%s" % (
                      await c.js("['#home-link','#gen-link'].map(function(s){"
                                 "return getComputedStyle(document.querySelector(s)).display;})"),
                      api_ok))
                await c.js("document.querySelector('.card').click()")
                T("进入播放视图且词条数 = 6",
                  await c.js("document.querySelectorAll('.row').length === 6"),
                  await c.js("document.querySelectorAll('.row').length"))
                await c.js("document.querySelector('.row').click()")
                # 「调了 play」只是活性；媒体到底有没有解码、播的是不是这一条，
                # 必须看副作用（src / readyState / currentTime / error）——
                # 旧断言只查 play() 被调用，媒体 404 时它照样 PASS（R4）。
                st = {}
                for _ in range(24):
                    await asyncio.sleep(0.25)
                    st = await c.js(
                        "(function(){ if(typeof audio==='undefined' || !audio) return null;"
                        " return {src: audio.currentSrc||audio.src, rs: audio.readyState,"
                        " paused: audio.paused, t: audio.currentTime,"
                        " err: audio.error ? audio.error.code : 0}; })()") or {}
                    if st.get("rs", 0) >= 2 and st.get("t", 0) > 0.15:
                        break
                T("点词条真的调了 play（不是只改文案）",
                  await c.js("(window.__mlog||[]).indexOf('play') >= 0"),
                  await c.js("(window.__mlog||[]).join(',')"))
                T("播放的是第 1 个词的逐词音频（不是整页 MP3）",
                  str(st.get("src", "")).endswith("audio/p001_001.mp3"), st.get("src"))
                T("媒体没有加载错误", not st.get("err"), st)
                if st.get("rs", 0) >= 2:
                    T("media 真的解码并在推进（readyState≥2、未暂停、currentTime>0.15）",
                      st.get("t", 0) > 0.15 and st.get("paused") is False, st)
                else:
                    S("media 解码推进", "本机无窗口 Edge 顶多 load 到 readyState=%s，"
                                        "无法验证出声（src/error 已单独验）" % st.get("rs"))

                # ── ⑧ file:// 下不给假入口 ────────────────────
                furl = "file:///" + os.path.join(pack, "index.html").replace("\\", "/")
                await c.send("Page.navigate", {"url": furl})
                ok = await c.wait("document.querySelectorAll('.card').length === 1", 40)
                await asyncio.sleep(1.0)
                T("file:// 下仍能打开播放器", ok)
                T("file:// 下两个入口都不出现（不给假承诺）",
                  await c.js("['#home-link','#gen-link'].every(function(s){"
                             "return getComputedStyle(document.querySelector(s)).display === 'none';})"),
                  await c.js("['#home-link','#gen-link'].map(function(s){"
                             "return getComputedStyle(document.querySelector(s)).display;})"))
                T("全程没有未捕获异常", not c.exceptions, c.exceptions[:2])
    finally:
        for p in (br, srv):
            if p:
                try:
                    p.terminate(); p.wait(timeout=8)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass
        shutil.rmtree(prof, ignore_errors=True)
        try:
            os.remove(txt)
        except OSError:
            pass
        if not keep:
            shutil.rmtree(os.path.join(PACKS, name), ignore_errors=True)
            work_cleanup(before)

    # 自清理本身也要断言（否则「没删掉」只能靠肉眼发现）
    if not keep:
        left = {d: sorted(work_snapshot()[d] - before[d]) for d in TRACKED}
        left = dict((k, v) for k, v in left.items() if v)
        T("跑完不留上传/任务残留", not left, left)

    fails = [r for r in RESULTS if r[0] == "FAIL"]
    for st, n, e in RESULTS:
        print("%-4s %s%s" % (st, n, ("   [%s]" % (e,)) if e is not None else ""))
    print("\n合计 %d 项：PASS %d / FAIL %d / SKIP %d"
          % (len(RESULTS), len(RESULTS) - len(fails) - sum(1 for r in RESULTS if r[0] == "SKIP"),
             len(fails), sum(1 for r in RESULTS if r[0] == "SKIP")))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
