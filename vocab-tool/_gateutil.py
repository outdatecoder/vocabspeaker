# -*- coding: utf-8 -*-
r"""门禁脚本共用的小工具：定位 Edge、定位项目根、了解门禁怎么跑。

为什么有这个文件：Edge / ffmpeg / 虚拟环境的路径曾经逐个写死在各脚本里，
换一台机器就全线失效，而失效方式是"跑完了、打一堆 0"而不是报错（R7）。
原则：**本机之外的东西不要写成常量，在调用点解析，并给出可读的失败信息。**

门禁怎么跑（与 AGENTS.md 第 3 节一致；先 cd 到 vocab-tool，再起服务）：

    $py = <解释器路径>                 # 开发机上写进 vocab-tool\local_python.txt
    cmd /c start_pet.bat --no-browser  # http://127.0.0.1:8901/

    & $py -X utf8 _probe_pack.py   "packs/初中英语词汇表"
    & $py -X utf8 _cdp_probe.py    "packs/初中英语词汇表" minibar
    & $py -X utf8 _cdp_inject.py   "packs/初中英语词汇表" "_trace.js" 40 x http 8901
    & $py -X utf8 _cdp_ui.py       "packs/初中英语词汇表"
    & $py -X utf8 _cdp_home.py
    & $py -X utf8 _api_probe.py
    & $py -X utf8 _e2e_generate.py

`_cdp_probe.py` / `_cdp_inject.py` **不打印 PASS/FAIL、退出码恒 0**：
必须自己读观测行（"底部条是否遮挡末条 = no"、"点底部条后 paused = true"、
播放中"高亮"与"idx"是否始终相等、"文件="是否换了词）。跑完 != 通过。
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
# serve.py 的 docroot 是 vocab-tool 的上级；_cdp_inject 拼 http 相对路径要用它
PROJECT_ROOT = os.path.dirname(HERE)

_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def edge_path():
    """返回 msedge.exe 绝对路径；找不到就抛可读的 SystemExit，而不是稍后空指针。

    顺序：环境变量 EDGE（显式指定优先）→ 两个常见安装位置。
    """
    env = os.environ.get("EDGE")
    cands = ([env] if env else []) + list(_EDGE_CANDIDATES)
    for p in cands:
        if p and os.path.isfile(p):
            return p
    raise SystemExit(
        "找不到 Edge：请设环境变量 EDGE=<msedge.exe 的完整路径>。已尝试：%s"
        % "；".join(str(p) for p in cands))
