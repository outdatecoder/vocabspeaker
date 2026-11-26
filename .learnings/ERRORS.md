# ERRORS

## [ERR-20261014-001] python -m http.server 不支持 HTTP Range → 音频整体不可 seek

**Logged**: 2026-10-14T18:00:00+08:00
**Priority**: high
**Status**: resolved
**Area**: infra

### Summary
用 `python -m http.server` 提供音频时，Chromium 把音频判为**不可 seek**，导致"点某条/上下词/暂停恢复"全部失效。

### Error
```
audio.seekable.length === 1   但   audio.seekable.end(0) === 0
→ audio.currentTime = t 被静默忽略，播放永远从 0 开始
```
（无报错、无异常，纯静默错误。表现：「点词条没反应」「总从第一条开始」。）

### Context
- 操作：通过 `http://127.0.0.1:8899/...` 预览词库包
- 关键对照：**双击 `index.html`（`file://`）完全正常**
- 原因：`SimpleHTTPRequestHandler` 既不响应 `Range` 请求，也不发 `Accept-Ranges: bytes`

### Suggested Fix
用支持 Range 的静态服务器（本仓库 `vocab-tool/serve.py`，纯标准库）：
```
python serve.py 8901     # 实测：Range 请求返回 206 + Content-Range: bytes 0-99/N
```
并在前端**显式检测并提示**不可 seek 的环境，不要静默降级。

### Metadata
- Reproducible: yes
- Related Files: `vocab-tool/serve.py`, `pet-player-sketch/index.html`
- Tags: http-range, media-seek, static-server, silent-failure

---

## [ERR-20261014-002] `play().then(() => pause())` 的「iOS 解锁」写法会掐掉真实播放

**Logged**: 2026-10-14T18:00:00+08:00
**Priority**: high
**Status**: resolved
**Area**: frontend

### Summary
首次点击「开始」无反应；必须先点暂停、再点开始才会播。

### Error
```js
// 旧代码
if (!unlocked){ unlocked = true; audio.play().then(() => audio.pause()).catch(()=>{}); }
audio.currentTime = t; audio.play();     // 同步发出，但会被上面的异步 pause 掐掉
```
`.then(() => pause())` 是**微任务**：同步的第二次 `play()` 已发出，随后 pause 落地 → 刚播就停。
而 `setPlaying(true)` 是同步执行的，所以按钮显示「⏸ 暂停」但实际静音 —— **探针只断言按钮文字时会被骗过**。

### Context
- 该写法本意是"在用户手势里解锁 iOS 音频"
- 但真实 `play()` 已在点击回调同步栈内，**它自己就完成了解锁**，额外 play/pause 纯属多余且制造竞态

### Suggested Fix
删掉解锁块。若 `play()` 因自动播放策略被拒，用 `NotAllowedError` 分支提示用户再点一次。

### Metadata
- Reproducible: yes
- Related Files: `pet-player-sketch/index.html`
- Tags: web-audio, promise-race, ios-autoplay

---

## [ERR-20261014-003] 用 `audio.seeking === true` 提前清除 pendingSeek，导致高亮/位置被拽回

**Logged**: 2026-10-14T18:00:00+08:00
**Priority**: high
**Status**: resolved
**Area**: frontend

### Summary
"点某条重听 → 从第一条开始"；连带"暂停再播放只能从第一条"、"上一词/下一词点了没用"。

### Error
```js
if (audio.seeking || Math.abs(audio.currentTime - pendingSeek) < 0.3) pendingSeek = null;
// audio.seeking 在赋值当帧就是 true → 等于立刻清掉，记账形同虚设
```
清掉之后 `tick()` 用 `audio.currentTime` 算 `trackAt()`：
- **Chrome**：seek 期间读回**新值** → 看起来正常（所以我用 Edge 测不出）
- **Safari 等**：读回**旧值** → 算到旧位置；旧位置≈0 时正好是**第 1 条**
→ `showTrack(0)` 把 `idx` 写成 0 并留下，后续 `playFrom(idx)` 就从第一条开始。

### Context
- 用户环境：Edge？不 —— 用户明确反馈在 Edge 上也出现，而我用 Edge 复现不出
- 复现手段：**CDP 劫持 `HTMLMediaElement.prototype.currentTime` 的 get/set**，模拟"seek 期间返回旧值"

### Suggested Fix
只在「读回来的位置确实等于目标（±0.35s）」时才清；查找当前条的 `trackAt()` 对 `!isFinite(t)` 必须保持不动而非返回 0。

### Metadata
- Reproducible: yes（需 shim）
- Related Files: `pet-player-sketch/index.html`, `vocab-tool/_cdp_probe.py`
- See Also: ERR-20261014-004
- Tags: media-seek, engine-difference, state-corruption

---

## [ERR-20261014-004] 靠"静音奇偶性"推断词边界 → 整体错位，先读出上一个词的中文

**Logged**: 2026-10-14T18:00:00+08:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Summary
"暂停 → 上一词/下一词 → 播放"会先读了**上一个词的中文**，再读选定的词。

### Error
```python
GAP_EC = 0.5      # 英↔中间隔
GAP_WORD = 0.5    # 词↔词间隔   ← 两者等长，根本无法区分

inter = [(starts[i], ends[i]) for i in range(1, len(starts), 2)]   # 赌"静音严格交替"
```
实测该页检出 **126 段静音（应为 113）**，片段内部停顿被多检出 → **奇偶性翻转** →
之后所有词边界都取到「英↔中」那一段 → `start` 指向该词中文起点。

### Context
- 误判链：先按"和实现相同的下标公式"核对，得出 56/56 吻合（**循环论证**，见 LRN-20261014-001）
- 改用内容判据（词边界必须是**长**静音）后立刻暴露
- 试过"按拼接顺序累加帧数"，又踩 ERR-20261014-005

### Suggested Fix
**不要再推断时间轴**：改为「一词一个 MP3」，播放某词就是播那个文件（`tracks[k].url`）。

### Metadata
- Reproducible: yes
- Related Files: `vocab-tool/build_vocab.py`, `vocab-tool/_check_timeline.py`
- See Also: ERR-20261014-005, LRN-20261014-001
- Tags: timeline-inference, silence-detection, parity-fragility

---

## [ERR-20261014-005] MP3 多段 `-c copy` 直拼：输入帧数之和 ≠ 输出帧数

**Logged**: 2026-10-14T18:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: backend

### Summary
想用"按帧累加"算精确时间轴，结果对不上，一页差 3.27s（改重编码后差 9.07s）。

### Error
```
ffmpeg -f concat -safe 0 -i list.txt -c copy out.mp3      → 解析式总长比实际多 3.27s
ffmpeg ... -c:a libmp3lame -ar 24000 -b:a 48k out.mp3     → 差 9.07s（更糟）
```

### Context
- 假设"输出 = 各输入帧的字节级串联"不成立：每段自带 LAME gapless 元数据（编码器延迟/填充），
  拼接处会被裁掉一点
- 该结论也是"换成逐词文件、彻底不需要时间轴"的直接动因

### Suggested Fix
若必须拼长音频又要精确定位 → **别用位置推断**，改成"一个语义单元一个文件"。
若只是要一个可播放的整页文件（如下载用），`-c copy` 无妨。

### Metadata
- Reproducible: yes
- Related Files: `vocab-tool/build_vocab.py`
- See Also: ERR-20261014-004
- Tags: mp3, gapless, concat-demuxer, frame-counting

---

## [ERR-20261014-006] 无 BOM 的 UTF-8 `.ps1` 被 PowerShell 5.1 按 ANSI 读，中文破坏引号

**Logged**: 2026-10-14T18:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: config

### Summary
含中文的 `.ps1` 直接报 `Unexpected token`，报错处的中文全是乱码。

### Error
```
At _word_pages.ps1:46 char:45
+   Write-Output ("鈥﹀叡 {0} 椤? -f $byPage.Count)
Unexpected token ')' in expression or statement.
```

### Context
- 文件以无 BOM UTF-8 保存 → PowerShell 5.1 按系统 ANSI(GBK) 解码 → 中文引号被破坏
- 同类坑：把 `$var` 写进命令行 `-Command "...$var..."` 时被环境吞掉

### Suggested Fix
1. 脚本内**只用 ASCII** 输出（或显式存成带 BOM 的 UTF-8）；
2. 复杂 PowerShell 写成 `.ps1` **文件**再 `-File` 执行，不要把带 `$` 的代码塞进 `-Command`。

### Metadata
- Reproducible: yes
- Related Files: `vocab-tool/_word_pages.ps1`, `vocab-tool/_restart_serve.ps1`
- Tags: powershell, encoding, bom

---

## [ERR-20261014-007] Windows `SO_REUSEADDR`：新旧服务器抢同一端口，旧进程继续应答

**Logged**: 2026-10-14T18:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: infra

### Summary
换了服务器代码却"没生效"：新的起得来，但请求仍由**旧进程**应答。

### Error
```
启动日志：serving ... (Range: on)          ← 新服务器报成功
实测请求：Accept-Ranges = None, Range 请求返回 200 全量   ← 其实还是旧的在答
```

### Context
- `ThreadingHTTPServer.allow_reuse_address = 1`（默认）→ Windows 上允许重复绑定同一端口
- 我按 `Get-NetTCPConnection` 报的 PID 去杀，杀到的不是真正应答的那个

### Suggested Fix
**换端口（如 8899 → 8901）比抢端口快**；排查前先用
`(Invoke-WebRequest).Headers['Accept-Ranges']` 之类的**结果特征**确认"到底谁在答"。

### Metadata
- Reproducible: yes
- Related Files: `vocab-tool/_restart_serve.ps1`
- Tags: windows, so_reuseaddr, port-conflict

---

## [ERR-20261014-008] headless + `--virtual-time-budget` 无法验证媒体行为

**Logged**: 2026-10-14T18:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: tests

### Summary
探针里音频永远 `readyState=0`，导致"是否真的出声"这类断言只能标 SKIP —— 而两个真 bug 正藏在这个盲区里。

### Error
```
SKIP  首次点击后真的在播   [headless 媒体未加载 readyState=0，需真机上用耳朵确认]
```
`--dump-dom` + `--virtual-time-budget` 会**快进计时器但不会快进真实 I/O**。

### Context
- 连带发现：探针原本只断言"按钮文字变成暂停"，被 ERR-20261014-002 的假暂停骗过

### Suggested Fix
1. 用 **CDP 驱动真实浏览器**（`--remote-debugging-port` + aiohttp WebSocket），跑**真实时间**断言；
2. 不要为了好看把 SKIP 报成 PASS；
3. 页面内部状态（顶层 `let`）可从注入脚本直接读，比截图/看按钮文字可靠得多。

### Metadata
- Reproducible: yes
- Related Files: `vocab-tool/_cdp_probe.py`, `vocab-tool/_cdp_inject.py`
- Tags: headless, virtual-time, media-testing, cdp
