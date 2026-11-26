# -*- coding: utf-8 -*-
"""核对时间轴（**内容判据**，不用实现里的下标公式，避免循环论证）。

原理：拼接结构是  [en][GAP_EC][cn][GAP_WORD] × N
- GAP_EC（英↔中）与 GAP_WORD（词↔词）本身都是 0.5s，但两侧片段自带的首尾静音不同，
  检测出来会形成**两个长度簇**（实测 ≈1.11s 与 ≈1.31s）。
- 真正的「词边界」必须是**长**静音的边界；若某个词的 start 落在**短**静音的终点上，
  那就是错位 —— 该 start 实际指向「上一个词的中文起点」。

用法：python _check_timeline.py "packs/初中英语词汇表_第1页"
"""
import io
import json
import os
import re
import subprocess
import sys

import imageio_ffmpeg

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
# 不写死某个虚拟环境里的二进制路径：$FFMPEG 直通，否则让 imageio_ffmpeg 自己找
FF = os.environ.get("FFMPEG") or imageio_ffmpeg.get_ffmpeg_exe()

pack = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "packs/初中英语词汇表_第1页")
d = json.load(io.open(os.path.join(pack, "pack_data.json"), encoding="utf-8"))
page = sorted(d["audio"].keys(), key=int)[0]
mp3 = os.path.join(pack, d["audio"][page]["mp3"].replace("/", os.sep))
tracks = d["audio"][page]["tracks"]

# 一词一 MP3 的新包，tracks 里是 url，没有整页时间轴；本脚本核对的是
# tracks_from_gaps() 时代的旧结构，硬跑只会 KeyError: 'start'（2026-11-26 实测）。
if tracks and "start" not in tracks[0]:
    # SKIP 走 stdout + rc=0：写到 stderr 会被 PowerShell 当成 NativeCommandError，
    # 看起来像"挂了"，而它其实只是没有可核对的对象。
    print("SKIP：这个包的 tracks 用 url 字段（一词一 MP3），"
          "本脚本只核对旧的「整页拼接时间轴」，此处没有可核对的对象。")
    sys.exit(0)

out = subprocess.run([FF, "-hide_banner", "-i", mp3, "-af",
                      "silencedetect=noise=-40dB:d=0.30", "-f", "null", "-"],
                     capture_output=True, text=True, encoding="utf-8",
                     errors="replace").stderr
dur_m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
dur = int(dur_m.group(1)) * 3600 + int(dur_m.group(2)) * 60 + float(dur_m.group(3))
st = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", out)]
en = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", out)]
sil = [(st[i], en[i], en[i] - st[i]) for i in range(min(len(st), len(en)))]

print("包：%s  词条 %d  时长 %.2fs" % (os.path.relpath(pack, HERE), len(tracks), dur))
print("检测到静音 %d 段（d>=0.30s）；期望 %d 段（%d 短 + %d 长）"
      % (len(sil), 2 * len(tracks) - 1, len(tracks), len(tracks) - 1))

lens = sorted(s[2] for s in sil)
mid = (lens[len(lens) // 2 - 1] + lens[len(lens) // 2]) / 2.0 if len(lens) > 1 else lens[0]
lo = [x for x in lens if x <= mid]
hi = [x for x in lens if x > mid]
threshold = (max(lo) + min(hi)) / 2.0 if lo and hi else mid
print("静音时长两簇：短 %.2f–%.2f（%d 段） / 长 %.2f–%.2f（%d 段）→ 判别阈值 %.2fs"
      % (min(lo), max(lo), len(lo), min(hi), max(hi), len(hi), threshold))
print()

LONG = [s for s in sil if s[2] > threshold]
print("按「长静音 = 词边界」检查每条 track：")
bad_start, bad_end, ok = [], [], 0
for t in tracks[1:]:
    hit = [s for s in LONG if abs(s[1] - t["start"]) < 0.10]
    if hit:
        ok += 1
    else:
        near = min(sil, key=lambda s: abs(s[1] - t["start"]))
        bad_start.append((t["i"], t["start"], near[1], near[2]))
for t in tracks[:-1]:
    if not [s for s in LONG if abs(s[0] - t["end"]) < 0.10]:
        near = min(sil, key=lambda s: abs(s[0] - t["end"]))
        bad_end.append((t["i"], t["end"], near[0], near[2]))

print("  start 落在长静音终点的条数 : %d / %d" % (ok, len(tracks) - 1))
print("  错位的 start 条数          : %d" % len(bad_start))
print("  end 不在长静音起点的条数    : %d" % len(bad_end))
if bad_start:
    print()
    print("  错位明细（词号 | 声明 start | 最近静音终点 | 该静音时长）:")
    for i, a, b, c in bad_start[:12]:
        print("    %-4d %8.2f %10.2f   %.2fs" % (i, a, b, c))
if bad_end:
    print()
    print("  end 异常明细（词号 | 声明 end | 最近静音起点 | 时长）:")
    for i, a, b, c in bad_end[:12]:
        print("    %-4d %8.2f %10.2f   %.2fs" % (i, a, b, c))

print()
print("长静音段数 = %d（应为 %d），短静音段数 = %d（应为 %d）"
      % (len(LONG), len(tracks) - 1, len(sil) - len(LONG), len(tracks)))
print()
if not bad_start and not bad_end:
    print("结论：✅ 时间轴与音频内容一致，没有错位。")
elif bad_start:
    print("结论：🔴 有 %d 条 start 落在【短静音】终点上 —— 这些词的 start 实际指向"
          % len(bad_start))
    print("      「上一个词的中文起点」，正是用户听到的现象。")
else:
    print("结论：🟡 start 都对，但有 %d 条 end 不落在词边界静音起点上。" % len(bad_end))
