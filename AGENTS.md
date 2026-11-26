# AGENTS.md — PET 词汇转语音应用

> 给在本仓库工作的 AI agent。**这里写的是"动手前的动作规则"，不是事故复盘。**
> 事故的完整来龙去脉见 `.learnings/`（`ERRORS.md` / `LEARNINGS.md`）。

---

## 0. 这个项目是什么

把英语词汇表（`docx` / `pdf`）转成**可循环听写**的**离线网页词库包**：
文档 → 抽取词条 → edge-tts 合成 → 拼装成单页 HTML 播放器 + 音频。

- 工具链：`vocab-tool/`（Python，运行环境 `D:\WorkBuddy\.workbuddy\binaries\python\envs\pet\Scripts\python.exe`）
- 入口是一条闭链：`vocab-tool/start_pet.bat` → `serve.py` → **`/`（词库工作台）** → 卡片进包 / 进 `/generator`；
  编排层在 `vocab-tool/webgen.py`（不 import HTTP，可被探针直接调用）
- **文档不在代码盘上**：计划书、`.learnings/`（`ERRORS.md` / `LEARNINGS.md`）与交接文档
  `交接_20261126.md` 都在 IDE 工作区 `c:\Users\Pengc\CodeBuddy\20261012091910\`
  （`Test-Path` 实测：D 盘没有 `.learnings/`，别在 D 盘找）。
- 本文件有**两份副本**（代码盘根目录 + 工作区同名文件）：改完必须**字节一致**（`sha256` 相同），
  否则下一轮会有 agent 按旧规则干活。2026-11-26 已合并过一次分叉（当时基线是工作区那份）。
- 产出文档统一写到 `D:\WorkBuddy\3-知识蒸馏与技能建设\`，再拷一份到工作区以便预览

---

## 1. 交付形态（别改错方向）

```
vocab-tool/packs/<词库名>/
├── index.html          ← 用户双击这一个
├── pack_data.json
└── audio/
    ├── p001_001.mp3 …  ← **每个词一个文件**（播放就播它，不做任何时间轴 seek）
    └── p001.mp3        ← 整页 MP3，**仅供「下载本页 MP3」**
```

- **目标使用场景是双击 `index.html`（`file://`）**，不是部署网页服务。任何设计先问"file:// 下还能用吗"。
- **一个包可以包含多页**：同一本书逐页 `build --merge-into <包目录>` 追加（同页号覆盖、幂等），
  列表页显示「共 N 页」。换一本新词表必须新建独立包，**不要**并入现有合集。
- **工作台（`/`）与生成器（`/generator`）属工具层**，只在本机服务里存在、**永不写进词库包**。
  包到工具层只靠 `fetch('/api/packs')` 成功与否决定是否显示入口（失败即隐藏，R6 不做假承诺）；
  工具层到包只用普通 `<a href>`。`file://` 打开这两页时都不渲染任何"点了没反应"的按钮。
- 直接访问 `packs/` 下任何目录一律 **302 回 `/`**：几百个 mp3 不该变成可浏览目录。

---

## 2. 七条硬规则（都违反过，都出过事）

### R1 · **验证必须独立于实现**
用与实现**相同的公式/函数**去核对它自己，永远是绿的 —— 那种"通过"没有信息量。
写验证前问两句：
1. 这个判据能不能被**同一个 bug** 骗过？
2. 我的"期望值"来自**独立真值源**，还是来自被验证的那段实现？

> 反例：我曾用 `tracks_from_gaps()` 里的同一个下标公式去核对它的输出，得出"56/56 吻合"，
> 而实际有 46 条错位。改用独立的内容特征（真词边界必须是**长**静音）后立刻暴露。

### R2 · **断言必须落在被验证的那一段上**
日志/输出**截断或筛选之后不许下结论**。写下"某环境表现一致"这类判断前，
必须能指出**支撑它的具体行**。

> 反例：我用 `Select-Object -First 12` 只看了追踪的前 12 行，就断言"HTTP 与 file:// 一致"，
> 而真正的证据在后段的暂停恢复阶段 —— 害我多绕一整轮并向用户传达了错误结论。

### R3 · **让结构不含歧义，优于事后推断**
发现自己在为"恢复被压缩/被推断掉的信息"不断打补丁时，**改数据结构**比继续调算法划算。
只要推断参与定位，就一定会错。

> 反例：`GAP_EC == GAP_WORD == 0.5` 无法区分，却靠"奇数下标"赌静音奇偶性 → 内部停顿一多就翻转。
> 正解：**一词一个 MP3**，歧义从结构上消失，播放器还净减了几十行。

### R4 · **活性与正确性分开断言**
"有反应"和"反应的是对的那一个"是两件事，必须两套断言。
观测点要选在**副作用**上（`HTMLMediaElement.play/pause` 调用序列、实际 `src`、内部 `idx`），
**不要只看 UI 文案**。

> 反例：旧探针只断言"按钮文字变成『暂停』"，而 bug 的表象恰是**按钮说暂停、音频却静音** ——
> **11 PASS / 0 FAIL 却漏掉 2 个真 bug**。

### R5 · **环境差异要变成可执行用例**
一旦意识到"我这个环境测不出某个差异"，**当场**把它实现成可注入的 shim / mock 并纳入门禁。
**不允许只留一句注释。**

> 反例：我明知"Edge 在 seek 期间立刻返回新值、Safari 返回旧值，所以我测不出"，
> 却只写进了文档 → 下一轮用户报"没解决"，我又在 Edge 上验证、又没抓到。

### R6 · **能力检测不能只看 `typeof`**
Web API 常"函数存在、调用才抛错"。判断能力要结合**上下文条件**，
真正的分支用 **`try/catch` 在调用点**决定，并**如实回报结果**；文案宁可少承诺。

> 反例：`typeof showSaveFilePicker === 'function'` 在 `file://`（opaque origin）下**也为 true**，
> 只有调用时才抛 `SecurityError` → 下载提示会**错误承诺**"文件夹可以自己选"（靠截图自查才发现）。

### R7 · **交付给别人用的诊断工具，自己先跑一遍自验**
探针、调试浮层、URL 开关都属于"排查用的工具"，它们自身出 bug 会把排查方向带偏。
取值无结果时，要能区分"**没有输出**"和"**取不到**"。

> 反例：调试浮层只设了 `className` 没设 `id`，而我用 `getElementById` 取值 →
> 误判"`d` 键没反应"，还去怀疑用户的键盘环境。

---

## 3. 常用命令

```powershell
$py = "D:\WorkBuddy\.workbuddy\binaries\python\envs\pet\Scripts\python.exe"
cd D:\WorkBuddy\2026-10-12-08-47-56\vocab-tool

# ① 取真实分页（一次性；39 页 / 2182 条约 22 秒）
#    缓存落在 work/_pagescan/<源sha20>_<脚本sha8>.json，另存 <源sha20>.pages.json 供解析用；
#    缓存键 = sha256(源文件) + sha256(_word_pages.ps1) + Word 版本 → 换文件/改脚本自动失效，不用手清
powershell -NoProfile -ExecutionPolicy Bypass -File _word_pages.ps1 "<docx>" "word_pages.json"
#    想知道为什么是这个速度：先跑 _bench_word_com.ps1 "<docx>" 看各原语耗时

# ② 解析某一页（N = Word 真实页码）
& $py build_vocab.py parse --in "<docx>" --out "work\第7页" --word-pages word_pages.json --page 7

# ③ 第一次生成词库包（换一本新词表也用它：--name 用新名字、新建独立包）
& $py build_vocab.py build --work "work\第7页" --name "初中英语词汇表" --out packs

# ④ 同一本书追加/更新某一页（同页号覆盖、幂等；不碰其它页音频）
& $py build_vocab.py build --work "work\第7页" --name "初中英语词汇表" --merge-into "packs/初中英语词汇表"

# ⑤ 只改播放器模板后重渲染（读 pack_data.json，不重跑 TTS / concat）
& $py build_vocab.py render --pack "packs/初中英语词汇表" --name "初中英语词汇表"

# ⑥ 预览（**必须用支持 Range 的服务器**）
powershell -NoProfile -ExecutionPolicy Bypass -File _restart_serve.ps1 8901

# ⑦ 交付前门禁（七条都要过）
& $py -X utf8 _probe_pack.py    "packs/初中英语词汇表"
& $py -X utf8 _cdp_probe.py     "packs/初中英语词汇表" minibar
& $py -X utf8 _cdp_inject.py    "packs/初中英语词汇表" "_trace.js" 40 x http 8901
& $py -X utf8 _cdp_ui.py        "packs/初中英语词汇表"
& $py -X utf8 _cdp_home.py      # 工作台 /：卡片顺序·规模行·两个入口·改名/删除·目录 302·移动端只读
& $py -X utf8 _api_probe.py     # 接口层：token 分级 / 上传拒绝 / 分组 / 构建原子替换 / 取消
& $py -X utf8 _e2e_generate.py  # 真浏览器：选文件→校对→生成→播放（含请求体比对）

# ⑧ 一键起服务（双击也行；--no-browser 只起服务不开浏览器）
cmd /c start_pet.bat --no-browser   # → http://127.0.0.1:8901/  （工作台；生成器在 /generator）
```

**门禁脚本要用 `python -X utf8` 跑**：控制台是 GBK，直接 `print` 中文/符号会 `UnicodeEncodeError`。

**门禁脚本已经在 git 里**（`vocabspeaker/vocab-tool/`，与开发目录字节一致），路径都在运行期解析：
Edge 认环境变量 `EDGE`（否则依次试两个常见安装位置），ffmpeg 认 `FFMPEG`（否则 `imageio_ffmpeg.get_ffmpeg_exe()`）。
共用逻辑在 `_gateutil.py`。**别再把 `C:\Program Files\...` 或某个虚拟环境的绝对路径写回脚本** ——
写死时的失败方式是"跑完了、打一堆 0"，不是报错（R7）。

**改完产品/门禁文件要同步到发布仓库**（`vocab-tool` 在开发目录与 `vocabspeaker` 各一份，必须字节一致）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File vocabspeaker\_sync_repo.ps1         # 只校验；exit 1 = 有差异
powershell -NoProfile -ExecutionPolicy Bypass -File vocabspeaker\_sync_repo.ps1 -Sync   # 按方向复制后自检
```

方向由脚本里三组清单决定，**不要随手反转**：
① 代码类一律 dev → repo；
② `README.md` 反过来（repo → dev），它是 GitHub 首页、跟 Release 一起维护；
③ 工作区 `*.md` → `repo\docs\`（`.learnings\*.md` → `repo\.learnings\`），`AGENTS.md` 再从 dev 刷到仓库与工作区。
脚本还挡三件事：源文件 **0 字节**时拒绝覆盖（0 字节 `start_pet.bat` 曾躺在文档目录里十天）；
`repo/vocab-tool` 出现清单外文件报 `EXTRA`（新增门禁脚本要登记进清单）；
文档根不存在时打 `SKIP docs` 并说明原因，**不假装通过**。

**注意 `_cdp_probe.py` / `_cdp_inject.py` 是「报值」脚本，不打印 PASS/FAIL**：退出码永远是 0，
"跑完了"不等于"通过了"。必须自己读那几行观测值（`底部条是否遮挡末条 = no`、
`点底部条后 paused = true`、播放中 `高亮` 与 `idx` 是否始终相等、`文件=` 是否换了词）。
只截末尾几行就下结论 = R2。

**参数语义别混**：
- `--word-pages` + `--max-pages N` = **真实第 N 页**
- `--max-words N` = 单纯按条数截断，**与分页无关**（它就是"40 词"误会的来源）

---

## 4. 本机环境陷阱（Windows，实测）

| 陷阱 | 现象 | 正确做法 |
|------|------|----------|
| `python -m http.server` **不支持 HTTP Range** | 音频被判**不可 seek**（`seekable.end(0) === 0`），"点某条/上下词/暂停恢复"全失效，且**静默无报错** | 用 `vocab-tool/serve.py`（纯标准库，返回 `206 + Content-Range`） |
| Windows `SO_REUSEADDR` | 新旧服务器**同时监听同一端口、旧进程继续应答**（表现为"改了代码却不生效"） | **换端口**（8899 → 8901）比抢端口快；先确认"到底谁在应答" |
| `start_pet.bat` 撞上已在应答的 8901 | 脚本先 `call :probe` 打 `/api/packs`，**200 就 `goto reuse` 直接复用旧进程**（`start_pet.bat:34-49`）：改了 `serve.py` 却仍跑旧代码，屏幕还写着"已在服务，复用"像是成功 | 改完代码必须真重启：照本节末**「跑门禁前的例行检查」**认 PID；别把"already serving"当成"改动已生效" |
| CDP 调试端口**写死在脚本里**（`_cdp_probe` 9333 / `_cdp_shot` 9444 / `_cdp_inject` 9555 / `_cdp_ui` 9556；只有 `_cdp_home`、`_e2e_generate` 用 `free_port()` 动态取） | 上一次探针崩了但 `msedge.exe` 没退 → 端口仍被占，新实例的调试端口起不来，脚本的 `/json/list` 就落到**旧浏览器**上，读回上一轮的结果（"改了代码却没生效"的另一条成因）；同一门禁并发跑必互撞 | 开跑前查这几个端口（见本节末命令），有监听就 `Stop-Process` 掉对应 `msedge`；**两个 CDP 门禁不要并行** |
| Edge profile 写成**固定目录**（曾用 `vocab-tool\_edgeprof_probe`） | 每跑一次探针就写 8~60 MB 进去，删了下一次又长回来（实测：删完跑一次 `_probe_pack`，立刻重建 `246 files / 6.8 MB`）；两个 probe 并行还会共用同一个 `-user-data-dir`，Edge 按单实例处理，第二个可能直接退出 → 结果不可信 | 2026-11-26 已改成 `tempfile.mkdtemp(prefix="edge_probe_")` + `finally: shutil.rmtree(...)`（`_probe_pack.py`），7 个探针现在都不在仓库目录留 profile；`.gitignore` 只留 `_edgeprof*/` 兜底。**新写探针一律照这个来** |
| `.ps1` 无 BOM UTF-8 | PowerShell 5.1 按 GBK 解码，中文破坏引号 → `Unexpected token` | 脚本内**只用 ASCII**；复杂脚本写成 `.ps1` **文件**执行 |
| 命令行 `$var` | 传进 `-Command "...$var..."` 时被吞掉 | 同上，落成文件 |
| headless Edge **加载不了媒体** | 探针里 `readyState` 恒为 0，"是否真出声"验不了 | 用 `_cdp_inject.py`+`_cdp_probe.py` 走 **CDP 真实浏览器**；不可验的要标 **SKIP**，不许谎报 PASS |
| `imageio_ffmpeg.get_ffmpeg_exe()` | 中文 Windows 下抛一次无害的 `UnicodeDecodeError: 'gbk' ... 0xac`（子线程，不影响功能） | 已加结果缓存；也可用环境变量 `FFMPEG` 直通跳过探测 |
| docx 无分页信息 | XML 里 `lastRenderedPageBreak` 0 处、显式分页符 0 处 | 用 **Word COM** 读 `wdActiveEndPageNumber`（`_word_pages.ps1`），**不要靠估算** |
| Word `Range.Information(3)` = **段尾页**，不是段首页 | 一条词条的中文末行溢到下一页时，整条被记成下一页：`232 grandpa …（口语）爷爷，外公` 因此被归到第 5 页，第 4 页只剩 56 词、末尾停在 grandfather | `_word_pages.ps1` 把 range **collapse 到段首**再取页写进 `page`，同时把段尾页写进 `page_end`；解析只按 `page` 分组。改脚本后 sha256 变化会自动让 pagescan 缓存失效（无需手工清） |
| 控制台 GBK | 第三方脚本 `print` emoji / `ℹ` 会 `UnicodeEncodeError`（如 `graph-subsection-precheck`） | 用 `python -X utf8 <script>` 跑；不要为此改别人的脚本 |
| `.bat` 里的 `timeout /t` | stdin 被重定向时直接 `ERROR: Input redirection is not supported` 退出 | 用 `ping -n 4 127.0.0.1 >nul` 当 sleep（双击与脚本调用都稳） |
| PowerShell 的 `[IO.File]` | `.NET` 用的是**进程**工作目录，不是 PS 的 `$PWD` → `ReadAllText("相对路径")` 报"找不到文件" | 一律传**绝对路径** |
| `_word_version()` 与 Word 自报版本 | 注册表给 `16.0.19328.x`，Word COM 给 `16.0`，拿两串比缓存键**永远不命中** | 判缓存只用注册表那串；Word 自报的只用于日志 |
| `Range.Information(3)` 单段 0.21s | 逐段取页号 → 2191 段要 **8 分钟**，界面像假死（这就是"取页数太慢"的根因） | `Content.Text` 取一次 + 每页 `GoTo` 一次（39 页约 19s），本地按字符偏移归页；见 `_word_pages.ps1` 头注释 |
| `_word_pages.ps1` 产出的 JSON **带 BOM** | PS 5.1 的 `-Encoding UTF8` 落盘 = UTF-8 **带 BOM**；`json.load(open(p, encoding="utf-8"))` 直接抛 `Unexpected UTF-8 BOM ... line 1 column 1`（2026-11-26 现场撞到） | 读它一律 `encoding="utf-8-sig"`（`build_vocab.py:498`、`webgen.py:738` 已经这么写）；临时脚本别用裸 `utf-8` |
| Word COM 调用的**顺序** | 先 `GoTo` ×39 再读 `Content.Text`：43.8s（每次 GoTo 都触发重排） | 先读一次 `Content.Text` 把排版“烫”好，再 `GoTo`：18.8s |
| `.bat` 里 `goto` 的目标标签写成 `::label` | `::` 在 cmd 里是**注释**不是标签 → `The system cannot find the batch label specified - start`；双击窗口一闪，表现成"点了没反应"，`call :probe` 一并失效。`start_pet.bat` 里 `:reuse/:start/:open/:end/:nopy/:probe` 六个全中，**换哪台机器都起不来** | 标签一律单冒号 `:label`；改完必须 `cmd /c <bat> --no-browser` **真跑一遍并读输出**（退出码或"跑完了"都不代表成功）；失败分支补 `if /i not "%~1"=="--no-browser" pause`，否则错误一闪就没 |
| Windows 内置 `tar.exe` 打 zip | `tar -a -c -f x.zip <dir>` 把**中文文件名按 CP437 重新编码**：`PET主题词汇` 存成 `\u2553\u2248\u2560\u0393…`，466 个条目全毁，解压后目录名不可还原（发布 v0.1.0 时实测，zip 里 `index.html` 都找不到） | 含中文路径的 zip 一律用 Python `zipfile`（非 ASCII 名自动带 UTF-8 标志 `0x800`）；验完必须用 `zipfile.namelist()` 对比 `codepoints` 与 `flag_bits & 0x800`，**不要用 `tar -tf` 判断**——控制台 GBK 下两者都显示乱码，分不清是显示问题还是数据坏了 |

### 跑门禁前的例行检查（约 30 秒，别省）

```powershell
netstat -ano | findstr :8901                                        # 谁在监听 8901 → 取 PID
Get-Process -Id <PID> | Select-Object ProcessName, StartTime, Path  # 是 serve.py 吗？是这次起的吗？
Get-NetTCPConnection -LocalPort 9333,9444,9555,9556 -State Listen   # CDP 端口有没有被上次的 msedge 占着
Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" |
  Where-Object { $_.CommandLine -match 'headless|remote-debugging-port' }   # 只找门禁遗留的实例
```

判据：`Path` 指向你自己的解释器、`StartTime` 是**本次**启动的时刻，才算"跑的是新代码"。
对不上就 `Stop-Process` 或换端口（8902 / 8903）；**在旧进程上测，测的是上一版。**

两个细节：
- `findstr` **没有输出时 rc=1**（"没匹配"），属正常 —— 在脚本里别把它当成失败。
- 清理只杀第 4 条查出来的 PID；**不要 `Stop-Process -Name msedge`**，那会连用户正在用的浏览器一起关掉。
  正常退出时探针会自己收尾（2026-11-26 实测：跑完 `_probe_pack` + `_cdp_ui` 后，无 headless/调试实例残留）。

> 为什么值得单独写一段：`SO_REUSEADDR`、`start_pet.bat` 的 reuse 分支、CDP 写死端口，
> 三者会造出同一个表征 —— "代码改了、现象没变、日志却是新的"。先认 PID，再谈逻辑。

---

## 5. 排查工具（优先用这些，别现造）

| 工具 | 用途 |
|------|------|
| `_probe_pack.py` | 包运行时结构断言；挂钩 `HTMLMediaElement.prototype.play/pause`，断言首次点击**只能调一次 play** |
| `_cdp_probe.py` | CDP 驱动真实 Edge 跑真实时间断言。**当前只保留 `minibar` 一个场景**（滚到底可暂停 / 不遮挡 / 返回列表自动隐藏）；原先为「整页 seek」写的 `playing`/`fresh`/`paused`/`safari` 已整体删除（见文件头说明） |
| `_cdp_inject.py` + 真实 `.js` | **外链注入**，可直接读页面顶层 `let`（`idx`/`tracks`/`playing`）。比巨型字符串 `Runtime.evaluate` 可靠得多 |
| `_cdp_ui.py` | **PC / 移动双态断言**（UA 覆写 + 视口 1440×900 / 390×844）。v1.3 起是**反向断言**：包内「添加词库 / 新增页」命令块已删干净（折叠标题 / `.cmd` / `.cmd-copy` = 0，渲染文本不含 `build_vocab`、`merge-into`、`支持的格式`、`添加词库`），PC 端容器为空且 `display:none`，移动端只剩「请在 PC 端上传与生成」；两种状态下 `input[type=file]` 与「上传」按钮数量必须为 **0**，列表卡片仍渲染、无横向溢出 |
| `_cdp_shot.py` | CDP 截图（指定视口 + 先滚到指定位置），检查布局遮挡。第一个参数也接受 `http(s)://` URL；模式带 `home` 前缀时**不点卡片/不点播放**，改等 `.pack` 渲染出来（截工作台用 `home` / `home-mid` / `home-bottom`，弹窗用 `home-dlg` / `home-dlg-dcd`）；带 `list` 前缀则截**包的列表页**不点进播放（`list-bottom` 看底部） |
| `_cdp_home.py` | **工作台门禁**（条数以脚本末尾那行 `N PASS / M FAIL / K SKIP` 为准，别拿旧数字当验收标准）：卡片数/首卡顺序/规模行文案 ← 另起 HTTP 读 `/api/packs` 自己算；点「打开播放器」真的进了包且渲染出 `.card`；包内两个入口可见；`?pack=` 已预选且提示不含「本次 0 页」；**提示里的起点「第 N 页起」必须等于 `max_page + 1`**（拿 `pages≠max_page` 的单页包 `_第2页` 做对照，没有这种包就如实 SKIP）；`packs` 目录被 302；**卡片操作区 `a[href]` 恰好 2 个 + 1 个「✎ 编辑」按钮**（打包下载接入那天前一条会主动失败）；**改名/删除用临时副本走真实点击**（改名后去磁盘 `index.html` 找 `<title>`、删除后查 `work/_trash`），跑完连临时包带 `_trash` 条目一起清；移动端只读 |
| `_bench_word_com.ps1` | **Word COM 原语计时**（open / ComputeStatistics / Content.Text / GoTo / Range.Text / Information(3) / Repaginate）。怀疑分页扫描慢，先跑它定位到具体原语，别凭感觉改 |
| `_gateutil.py` | 门禁共用：`edge_path()`（`EDGE` → 两个常见安装位置，找不到就抛可读的 SystemExit）、`PROJECT_ROOT`。**新写门禁时用它，别再写死路径** |
| `_check_timeline.py` | 用**内容判据**核对时间轴（刻意不用实现的下标公式）。**只适用旧结构**（`tracks` 带 `start`/`end`）：一词一 MP3 的新包会打印 SKIP 并 `rc=0` 退出，不是挂了 |
| `_api_probe.py` | 起一个真 `serve.py`，用真 HTTP 打全部接口：token 分级、上传六类拒绝、固定条数分组、构建的页号偏移、**并入后旧音频 SHA-256/文件数不变**、**并入重复词 → 409 且目标包不动**、**判据分级：`test_dup_grading` 六种形态（同页重复/整批重复/边界滑移 → 拦；书内跨单元重复 → 放行+提示），另有端到端用例（放行时 `dup_warnings` 进 job 与 result，重复那条与新词一起落进新页）**、取消、重启标 interrupted、Word 分页与缓存 |
| `_e2e_generate.py` | CDP 全链路：`DOM.setFileInputFiles` → 解析 → 改词/删行/筛选 → 选页 → 生成 → **再并一次必撞重复词并确认被拦在页面上** → 打开播放器点词条真的 `play()`；并在页面里包一层 `fetch` **抓 `/api/build` 请求体**与界面状态比对 |
| 顶栏 **版本号** + **`?debug=1`** | 消除"你看的是哪一版/哪一份文件"的歧义。**排查位置类问题时先让对方念这一行** |

---

## 6. 刻意的设计决定（不要"顺手优化"掉）

| 决定 | 原因 |
|------|------|
| **一词一个 MP3**，不做整页 seek | 见 R3；整页 seek 已连续引发 4 个缺陷 |
| 保留**整页 MP3** | 用户明确要求"下载是最初指定的功能"，不接受为省 1 MB 去掉 |
| 下载走**对话框**（可改文件名） | 用户要求文件名/位置可自选；`file://` 下浏览器不允许选文件夹，故按 `location.protocol` 如实告知并降级 |
| 顶栏**版本号**常驻 | 今天为此白跑过两轮 |
| `mp3_frames()` 自己**逐帧数时长** | 不用 ffmpeg 的 `Duration`（估算值，上百段会累积误差） |
| `tracks_from_gaps()` 保留但**不再用于新包** | 仅为兼容当年那份 PDF 草图数据；新包用 `url` 字段 |
| 词间停顿由**播放器**补（`WORD_PAUSE_MS`） | 逐词音频里不含尾部静音 |
| **不做网页上传控件** | 离线包没有服务端，放控件就是假承诺（R6）。PC 端新增/加页一律走顶栏「＋ 添加词表」进生成器，**包内不再贴任何命令**（v1.3 删掉了整块命令说明）；移动端只留一句「请在 PC 端上传与生成」 |
| **生成需联网，播放不需要** | edge-tts 是云端服务；只有词级缓存全命中时才不联网。播放阶段完全离线 |
| 生成器**先建 stage 再整体换**（`work/_stage/<job>` → `packs/<id>`） | `build` 直接写目标包，TTS/ffmpeg 中途失败就剩半套。Windows 不能 `os.replace` 覆盖非空目录 → 旧包先 rename 到 `work/_backup/<job>` |
| 并入时**页号统一加 offset**（`max(现有 page)`） | `build --merge-into` 是按页 upsert，新文件从第 1 页起会**覆盖**现有页 |
| 写接口要 `X-Token`，只读接口免 | 只绑 127.0.0.1 挡不住浏览器里别的页面 POST；但静态播放器要靠 `/api/packs` 探测「＋ 添加词表」，所以只读不能要 token |
| 校对表**折叠即释放 DOM** | 2182 词一次性插入会卡死；筛选后自动展开也封顶 200 行 |
| 「可疑」必须给**理由 + 建议**（点行尾 `?`），且**按内容即时重算** | 只画红框 = 让用户自己猜；`score` 是解析时的快照，改完还挂着旧标记就是误导。评分只在「没改过且内容看不出问题」时才作为提示 |
| 网页解析带 `--keep-low` | 命令行那套「低分页静默丢弃」在网页里等于偷偷删用户内容；交给校对表让用户自己删 |
| 未勾选真实分页时叫「**组**」不叫「页」 | 没有真实页码却写"第 N 页"是假信息；写进包之后才一律是"页" |
| 工作台卡片上的页数/词数/时长/体积**全部来自 `/api/packs`** | 前端不猜、不二次推导（R1）；每次进入重新拉取 + `no-store`，避免"生成完还看到旧数字" |
| `list_packs()` 跳过 `_` 开头的包目录 | 门禁/调试产物（`_probe_*`）不该出现在用户界面；缺这条会被自己的测试垃圾淹掉 |
| 卡片 `mtime` 取 `pack_data.json` 的时间 | 目录时间会被无关文件刷新，排序会乱跳 |
| 移动端**不渲染**生成入口（而不是 CSS 藏起来） | 藏起来仍可 Tab 到 / 点进去撞「请在 PC 端」两跳墙；判定用与生成器同一个 820px 闸门 |
| 「打包下载」**只留注释槽位**，不建路由、不渲染灰按钮 | 灰按钮 = 假承诺（R6）；门禁锁"恰好 2 个链接"，接入时必须一起改文档与门禁 |
| 删除词库 = **移进 `work/_trash/<id>__<时间戳>`**，不用 `rmtree` | 一个词库是几百个词 + 音频，误点一次就没了；移动同样让它在界面瞬间消失，却留了一次手工捞回的机会。界面文案照旧写"无法恢复"（用户不该指望它） |
| 改名后**必须重渲染 `index.html`** | `pack_data.json` 不存名字，播放器标题只在 HTML 里；漏了就是"文件夹新名、标题旧名"，旧名永久留在包里 |
| 改名/删除与构建**共用 `_target_lock`**，`acquire(blocking=False)` | 构建整段持锁；阻塞等待会让 HTTP 挂几分钟（表现成"点了没反应"），所以拿不到就 409 |
| 重命名**禁止 `_` 前缀 / 结尾点** | `_` 开头会被 `list_packs()` 当调试产物隐藏（用户以为词库没了）；结尾点在 Windows 上建不出目录 |
| 上传卡**不自动跳步**：选完文件只是「已选择」，必须点「开始解析」才上传+解析 | 传完就跳走，用户来不及看「按 Word 真实页码分页」这个开关 —— 而它决定「页」还是「组」，选错要整轮重来（`_e2e_generate.py` 现在会在 `setFileInputFiles` 后显式点 `#do-upload`） |
| docx 且未勾真实页码 → **开跑前**在上传卡给赭金提示 | R6：不能等校对表出来才说"这是组不是页"；此时用户还有机会勾上再开始 |
| 取消后同时给「**返回上一步**」与「重新开始」 | 返回上一步保留已选文件/勾选，改一下就能重来；重新开始才清空。生成阶段取消则退回「选目标」卡（`backStep()` 按 `S.buildJob` 判断） |

---

## 7. 交付前检查清单

- [ ] 解析时带了 `--word-pages`，并且 `--page N` 的条数与 Word 报的一致
- [ ] 页码映射里 `page != page_end` 的词条（跨页）已归到**起始页**：本库应恰好 2 条（`232` p4→5、`1225` p21→22）
- [ ] `_probe_pack.py` → 0 FAIL（SKIP 要能解释清楚）
- [ ] `_cdp_probe.py minibar` → 滚到底可暂停、不遮挡、返回列表自动隐藏
- [ ] `_cdp_inject.py _trace.js` → 点词条/上一词下一词/暂停恢复**高亮不漂移**
- [ ] `_cdp_ui.py` → 0 FAIL：包内命令说明已消失（`.cmd`/`.cmd-copy`/`build_vocab`/`merge-into`/`支持的格式` 一个不剩），PC 端容器收起、移动端只剩「请在 PC 端上传与生成」，且两端 `input[type=file]`/「上传」按钮数均为 0、卡片仍渲染
- [ ] 改了播放器模板后跑过 `render`（**不要**为改文案重跑整包 TTS / concat），并确认音频文件数不变
- [ ] 手动过一遍：暂停 → 下一词 ×2 → 播放（应直接读第三个词，**不带出上一个词的中文**）
- [ ] 预览用 `serve.py`，**不用** `python -m http.server`
- [ ] 包目录无 `_` 开头的临时文件残留
- [ ] `_cdp_home.py` → 0 FAIL：卡片顺序/规模行与 `/api/packs` 一致、两个入口可见、`?pack=` 已预选、目录 302、移动端只读、卡片操作区 2 链接 + 1 编辑按钮、**改名后磁盘 `<title>` 跟着变、删除后目录进 `work/_trash`**
- [ ] `_trash` 里没有本次门禁留下的 `probe_*`（门禁自己会清；留着说明它中途挂了）
- [ ] `_api_probe.py` → 0 FAIL（并入用例**用副本**，不许拿真包试；含「并入重复词 → 409 且目标包一个字节没动」）
- [ ] 并入前先问一句**单位对不对**：这份文件是「50 词一组」还是书的真实页？包是按哪种建的？错位必然重复词（`_dup_report` 只拦「边界滑移/整批重复」形态，零星跨页重复是放行的）
- [ ] `_e2e_generate.py` → 0 FAIL（含 `/api/build` 请求体与界面一致；**选页**：默认全选、取消勾选后「下一步」被禁用、勾回来恢复、提交的页号从 1 连续；**上传卡**：选文件后不自动跳步、docx 未勾真实页码给提示、勾上后提示消失）
- [ ] 手动过一遍**解析中取消**：出现「返回上一步」，点它回到上传卡且已选文件还在（e2e 里这条是 SKIP，7 个词的解析太快，取消窗口不可靠）
- [ ] 改了 `serve.py` / `webgen.py` 后，`start_pet.bat --no-browser` 起得来、`/generator` 200、`/api/packs` 200
- [ ] 改了播放器模板后，`packs/*/index.html` 都重渲染过（否则老包没有「＋ 添加词表」）
- [ ] `work/` 下无本次调试留下的 `*.txt` / 临时 `.js`（`_uploads`/`_jobs`/`_parse` 是设计目录，不算）
- [ ] 改了本文件后，三份 `AGENTS.md`（代码盘根目录 / `vocabspeaker\AGENTS.md` / IDE 工作区）`sha256` 相同 ——
      跑一次 `_sync_repo.ps1` 就一并刷好（以代码盘那份为准）。这一条自身就是 2026-11-26 合并分叉的直接原因
- [ ] `_sync_repo.ps1`（不带参数）**退出码 0**：dev 与 `vocabspeaker` 逐文件 `sha256` 相同，
      且没有 `EXTRA`（`repo/vocab-tool` 里出现清单外文件）。有差异先跑 `-Sync` 再复核

---

## 8. 网页工具层（`/` 与 `/generator`）的四条不变量

改这块之前先读这一节 —— 这些是**结构**约束，不是实现细节：

1. **接口分层**：`serve.py` 只做 HTTP（路由 / token 分级 / JSON / Range 静态），
   `webgen.py` 只做编排（上传、解析、分组、CSV、stage、锁、回滚、取消），
   解析与合成**只走** `build_vocab.py` 子进程。想在 `serve.py` 里加业务逻辑 = 加错地方。
2. **写盘只落在自己那几层目录**：
   `work/_uploads`（原文件）→ `work/_parse/<doc>`（`words.csv`）→ `work/_build/<job>`（待合成词表 + `source.json`）
   → `work/_stage/<job>`（完整暂存包）→ `packs/<id>`（整体换过去），撤销痕迹在 `work/_backup/<job>`。
   工作台删掉的词库进 `work/_trash/<id>__<时间戳>`（**移动，不是 `rmtree`**）。
   任何一步失败或取消，`packs/<id>` 必须**原样不动**，且不留 stage/backup。
3. **两个页面各说各的名字**：校对阶段叫「**组**」（无真实页码）或「**页**」（有），
   写进包之后一律叫「**页**」。跨这个边界时**只转换一次**，而且只在 `collectRows()` 里转：
   勾选的组按顺序压成 `1..k`（前端）→ 后端统一加 `offset = 现有 max_page`（`offset + page`）。
   别让「组 / 页」在同一段文案里混着用。
   **只勾一部分页也成立**：未勾选的组不提交、也不占号，所以包里不会出现空号或跳号。
   **单位不同就会重复词**：50 词一组 vs 书的真实页，边界一律对不上（第 2 组从第 51 个词开始，
   而第 1 页到第 57 个词 → 7 个词两页都有）。所以①目标卡在「组」并入时给赭金提醒，
   ②`webgen._dup_report()` 在**开跑前**按 `(en, cn)` 比对目标包，并且**按重复的形态分级**
   （`_dup_report` 返回 `(hard, soft, detail)`）：
   - 同一页内出现两条一模一样的词 → 409（勾重/表格重复粘贴，一页不会自己收两次）
   - 本批 ≥ `DUP_BLOCK_RATIO`(30%) 的词已在包里 → 409（把同一批词又并了一次）
   - 重复词**全部**同时是「包最后一页的词」∩「本批首页的词」且 ≥2 条 → 409（边界滑移）
   - 其余零星重复 → **放行**，清单写进 job 日志、`result.dup_warnings` 与完成页（不许静默）
   > `(en, cn)` 这个键分不开「分页错位」和「教材本身跨单元收了同一个词」——
   > 实测 look「瞧，看」在书第 2 页与第 8 页各一条、work「工作」在第 4 页与第 8 页各一条
   > （一个动词一个名词，读音相同）。一律 409 会让你**永远无法分批并入这本书**。
4. **工具层永不进包，包也不依赖工具层**：`home.html` / `generator.html` 只由 `serve.py` 现读现发
   （`_serve_page(filename, inject_token)`，`no-store`；**两页都注入 token** —— 工作台有改名/删除两个写操作）。
   页面里判定"有没有 token"只能比**前缀** `TOKEN.indexOf('__PET') !== 0`：`serve.py` 是全文替换占位符，
   写成完整占位符会被一起替换，判定恒假 → 所有写入口被静默隐藏（踩过一次）。
   词库包里的入口靠 `fetch('/api/packs')` 探测后才显示 —— 所以**这个接口必须免 token**，
   否则 `file://` 双击的包和移动端都会静默丢掉入口。反方向（工作台/生成器 → 包）只用 `<a href>`。

请求路由表（改动前对着这张表看）：

| 路径 | 行为 |
|---|---|
| `GET /`、`/home`、`/home.html`、`/vocab-tool/home.html` | `home.html`，**注入 token**（工作台有改名/删除两个写操作；见 `serve.py:197-199`） |
| `GET /generator`、`/vocab-tool/generator.html` | `generator.html`，注入 token |
| `GET /api/packs`、`/api/job/<id>`、`/api/preview` | JSON，免 token |
| 其它任何**目录** | `302 → /`（`list_directory` 一律打发回工作台） |
| 预留（本期不实现） | `GET /api/export?pack=<id>` → `application/zip`，免 token，复用 `webgen.resolve_pack_id()` 做越界检查；接入点见《词库工作台_实施方案》§8.4 |

接口一览（★ = 需 `X-Token`）：`GET /api/packs`、`GET /api/job/<id>`、`GET /api/preview?job=<id>`、
`POST /api/upload` ★、`POST /api/parse` ★、`POST /api/build` ★、`POST /api/cancel` ★、
`POST /api/pack/rename` ★（`{id, name}`：改目录名 + 强制重渲染）、`POST /api/pack/delete` ★（`{id}`：移入 `work/_trash`）。

`/api/packs` 每项字段：`id / name / pages / words / sec / bytes / mtime / max_page / url`。
**并入起点只认 `max_page`**（后端 `offset = max_page`，新页从 `max_page+1` 起），
`pages` 只是"有几页"。两者只在页号连续且从 1 开始时相等 —— 单页包 `_第2页` 是 `pages=1 / max_page=2`，
前端若用 `pages+1` 就会把"排到第 3 页"说成"第 2 页"（v1.8 修的就是这个）。

`PET_API_TOKEN` 环境变量只给门禁探针用（探针在另一个进程里，拿不到随机 token）；正常启动不要设。
