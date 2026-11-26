# LEARNINGS

> **已提升到项目级 `AGENTS.md`（2026-10-14）**
>
> 本文件全部 8 条（LRN-001 … LRN-008）已按"**预防规则**"形式提炼进工作区根目录的 `AGENTS.md`，
> 供未来 agent 会话自动生效：
>
> | 本条 | AGENTS.md 中的落点 |
> |------|-------------------|
> | LRN-001 验证独立于实现 | **R1** |
> | LRN-002 断言落在被验证的那一段 | **R2** |
> | LRN-006 让结构不含歧义 | **R3** |
> | LRN-005 活性与正确性分开断言 | **R4** |
> | LRN-003 环境差异变可执行用例 | **R5** |
> | LRN-004 能力检测不能只看 typeof | **R6** |
> | LRN-007 诊断工具先自验 | **R7** |
> | LRN-008 估算 vs 事实 | §3 参数语义 + §4 docx 无分页信息 |
>
> 下面保留**完整证据与背景**（含我自己的反例），供需要细节时回查。

## [LRN-20261014-001] verification_methodology

**Logged**: 2026-10-14T18:00:00+08:00
**Priority**: critical
**Status**: promoted
**Area**: tests

### Summary
**验证判据必须独立于实现。** 用与实现相同的公式/函数去核对它自己，永远是绿的 —— 那种"通过"没有任何信息量。

### Details
核对时间轴时，我直接复用了 `build_vocab.py` 里 `tracks_from_gaps()` 的同一个下标公式
（`ends[2*(i-1)+1]`）去比对它产出的结果，得出"56/56 吻合"，于是判定"对齐正确"。
**这是循环论证**：左边由公式生成，右边又用同一个公式去查，当然吻合。
改用与实现无关的**内容特征**（真词边界必须是约 1.3s 的**长**静音、英↔中是约 1.1s 的短静音）后，
立刻暴露：`start` 落在长静音终点的只有 **10/56**，46 条错位。

### Suggested Action
写验证代码前先问两句：
1. 这个判据能不能被**同一个 bug** 骗过？
2. 我的"期望值"是来自**独立真值源**，还是来自被验证的那段实现？
（若只能来自实现，就必须换一个可观测的外部特征。）

### Metadata
- Source: conversation
- Related Files: `vocab-tool/_check_timeline.py`, `vocab-tool/build_vocab.py`
- See Also: ERR-20261014-004, LRN-20261014-002
- Pattern-Key: verify.independent_oracle
- Recurrence-Count: 1
- First-Seen: 2026-10-14
- Last-Seen: 2026-10-14
- Tags: verification, testing, circular-reasoning

---

## [LRN-20261014-002] verification_methodology

**Logged**: 2026-10-14T18:00:00+08:00
**Priority**: high
**Status**: promoted
**Area**: tests

### Summary
**断言必须落在被验证的那一段上。** 截断/筛选输出之后再下结论，等于没验证。

### Details
排查"HTTP 下 seek 是否正常"时，我的追踪脚本输出了 A→D 四段，
但我在管道里只 `Select-Object -First 12`，看到开头 `A 进页`/`playFrom` 正常，
就写下"**HTTP 与 file:// 表现一致**"并写进了计划文档 —— 而真正的证据在后面的 **D 段**
（暂停恢复阶段），那里明明是 `seekable=1(到 0.0)`、seek 完全不生效。
结果同一个问题多绕了一整轮，且给用户传达了一个错误结论。

### Suggested Action
- 用日志排查时，**过滤条件要与断言位置对应**（断言 D 段就只看 D 段，且要看全）；
- 写下"某环境表现一致"这类结论前，明确列出**支撑它的具体行**；
- 探针/脚本把结论直接打印在断言旁边，减少人肉筛选。

### Metadata
- Source: conversation
- Related Files: `vocab-tool/_cdp_inject.py`, `vocab-tool/_trace.js`
- See Also: LRN-20261014-001, ERR-20261014-001
- Pattern-Key: verify.assert_on_the_verified_segment
- Recurrence-Count: 1
- First-Seen: 2026-10-14
- Last-Seen: 2026-10-14
- Tags: verification, log-truncation, false-conclusion

---

## [LRN-20261014-003] verification_methodology

**Logged**: 2026-10-14T18:00:00+08:00
**Priority**: high
**Status**: promoted
**Area**: tests

### Summary
**环境差异要变成可执行的复现，而不是写进注释。** 否则同一个坑会踩第二次。

### Details
第一轮我已明确意识到"Edge 在 seek 期间立刻返回新值，Safari 返回旧值，所以我用 Edge 测不出"，
但只把这个认知**写进了文档**、没有做成用例。第二轮用户报"问题没解决"，
我又一次在 Edge 上验证、又一次通过、又一次没抓到。
直到用 CDP **劫持 `currentTime` 的 get/set**、人为制造"seek 期间返回旧值"，
才做出可重复的复现（序列里出现 `0@xxx`）。

### Suggested Action
意识到"我这个环境测不出某个差异"时，**当场**把它实现为一个可注入的 shim / mock，
并纳入回归门禁；不允许只留一句注释。

> **2026-10-14 补充**：当时做的那个 `currentTime` get/set shim 已**随被测设计一起删除** ——
> 「整页 MP3 + 按时间轴 seek」被「一词一个 MP3」取代后，seek 语义差异不再存在，
> `_cdp_probe.py` 只保留 `minibar`（删了 5 个失效场景）。
> **但这条规则不变**：shim 会随设计过时而报废，而"把环境差异做成用例"这个动作永远要做。

### Metadata
- Source: conversation
- Related Files: `vocab-tool/_cdp_probe.py`
- See Also: ERR-20261014-003
- Pattern-Key: verify.encode_env_difference_as_test
- Recurrence-Count: 2（同一类问题连续两轮）
- First-Seen: 2026-10-14
- Last-Seen: 2026-10-14
- Tags: environment-difference, regression-gate, cdp-shim

---

## [LRN-20261014-004] api_capability_detection

**Logged**: 2026-10-14T18:00:00+08:00
**Priority**: high
**Status**: promoted
**Area**: frontend

### Summary
**能力检测不能只看 `typeof f === 'function'`。** Web API 常常"函数存在、调用才抛错"。

### Details
下载功能里我用 `typeof window.showSaveFilePicker === 'function'` 判断能否弹出"另存为"，
据此写文案。实测即使在 **`file://`（opaque origin）** 下该函数**也存在**，
只有调用时才抛 `SecurityError`。于是文案会向用户**错误承诺**"文件夹可以自己选" ——
是靠**截图自查**才发现的。

### Suggested Action
- 能力判断结合**上下文条件**（此处用 `location.protocol !== 'file:'`）；
- 真正的分支用 **`try/catch` 在调用点**决定，并让 toast 如实回报"实际存到了哪"；
- 文案宁可**少承诺**，也别承诺做不到的事。

### Metadata
- Source: conversation
- Related Files: `pet-player-sketch/index.html`
- Pattern-Key: api.capability_detection_context_aware
- Recurrence-Count: 1
- First-Seen: 2026-10-14
- Last-Seen: 2026-10-14
- Tags: web-api, feature-detection, file-system-access, ux-honesty

---

## [LRN-20261014-005] correctness_definition

**Logged**: 2026-10-14T18:00:00+08:00
**Priority**: high
**Status**: promoted
**Area**: tests

### Summary
**"能跑" ≠ "对齐"。** 从"没声音"到"有声音"是能跑；从"有声音"到"读的是对的那个词"才是对齐。

### Details
旧探针断言的是「点击后按钮文字变成『暂停』」。而 bug 的表象恰恰是
**按钮说暂停、音频却是静音** —— 断言选在了错误的观测点，所以
**11 PASS / 0 FAIL 却漏掉了 2 个真 bug**。
同样地，"音频能播"是能跑，"播出来的是第 30 条而不是第 1 条"才是对齐。

### Suggested Action
每加一个功能，同时明确两类断言：
- **活性**（有没有反应）
- **正确性**（反应的是不是那一个 / 那个位置）
并且观测点要选在**副作用**上（`HTMLMediaElement.play/pause` 调用序列、实际 `src`、内部 `idx`），
不要只看 UI 文案。

### Metadata
- Source: conversation
- Related Files: `vocab-tool/_probe_pack.py`
- See Also: ERR-20261014-002, LRN-20261014-001
- Pattern-Key: verify.liveness_vs_correctness
- Recurrence-Count: 1
- First-Seen: 2026-10-14
- Last-Seen: 2026-10-14
- Tags: assertion-design, observability

---

## [LRN-20261014-006] architecture

**Logged**: 2026-10-14T18:00:00+08:00
**Priority**: high
**Status**: promoted
**Area**: backend

### Summary
**让结构本身不含歧义，优于事后推断。** 当"靠算法恢复信息"反复出错时，改数据结构比调算法划算。

### Details
「整页一个 MP3 + 按时间轴 seek 到某词」要求时间轴与音频**逐词对齐**，
而时间轴只能**推断**（数静音、数帧），于是：
- 数静音：`GAP_EC == GAP_WORD` 无法区分，内部停顿一多就奇偶翻转（ERR-20261014-004）
- 数帧：`-c copy` 的 gapless 逐段裁切让"输入帧数≠输出帧数"，差 3.27s / 9.07s（ERR-20261014-005）
- 即便算对了，还得对抗"服务器不支持 Range 就不可 seek"（ERR-20261014-001）

改成「**一词一个 MP3**」后，播放某词就是播那个文件 ——
上述三类问题**同时消失**，播放器还删掉了整套 seek 机制（净减代码）。

### Suggested Action
当发现自己在为"恢复被压缩/被推断掉的信息"不断打补丁时，
评估一次**改数据结构**（多文件 / 显式索引 / 冗余字段）是否直接消灭歧义。

### Metadata
- Source: conversation
- Related Files: `vocab-tool/build_vocab.py`, `pet-player-sketch/index.html`
- See Also: ERR-20261014-004, ERR-20261014-005, ERR-20261014-001
- Pattern-Key: design.eliminate_ambiguity_over_inference
- Recurrence-Count: 3（同一假设连出三个问题）
- First-Seen: 2026-10-14
- Last-Seen: 2026-10-14
- Tags: architecture, data-structure, seek-elimination

---

## [LRN-20261014-007] debugging_tooling

**Logged**: 2026-10-14T18:00:00+08:00
**Priority**: medium
**Status**: promoted
**Area**: frontend

### Summary
**自查工具自身的 bug 会把排查方向带偏。** 给用户的诊断入口必须先自验。

### Details
我加的调试浮层 `document.createElement('div')` 只设了 `className = 'dbg'`、
**没设 `id`**，而取值用 `getElementById('dbg')` → 永远取不到。
于是我判定"`d` 键没反应"，并据此怀疑用户的键盘/预览环境 —— 实际是我的取值方式错了。
同理，第一版探针只断言按钮文字（见 LRN-20261014-005）也是"验证器错了"。

### Suggested Action
- 交付给用户使用的诊断工具（浮层、URL 开关、探针）**先跑一遍自验**；
- 取值优先用已确定的 id/选择器，并在无结果时**明确区分"没有输出"和"取不到"**；
- 让诊断信息**常驻可见**（如 `?debug=1` 自动开启），减少对键盘交互的依赖。

### Metadata
- Source: conversation
- Related Files: `pet-player-sketch/index.html`
- See Also: LRN-20261014-005
- Pattern-Key: debug.diagnose_tool_selfcheck
- Recurrence-Count: 2
- First-Seen: 2026-10-14
- Last-Seen: 2026-10-14
- Tags: debugging, self-check, observability

---

## [LRN-20261014-008] estimation_vs_fact

**Logged**: 2026-10-14T18:00:00+08:00
**Priority**: medium
**Status**: promoted
**Area**: docs

### Summary
**估算值必须标注为估算，并且不要让它悄悄变成事实。** "约一页 40 条"最终被当成"第 1 页 40 条"。

### Details
为了先跑通一页，我用了 `--max-words 40`，并在报告里写了"约一页容量（可调）"。
随后这个 40 在后续表述中逐渐等同于"第 1 页"，直到用户指出**真实第 1 页是 57 词**。
根因是"没有去核实真实分页"——而 docx 的 XML 里确实没有分页信息，
最后是用 **Word COM** 逐段读 `wdActiveEndPageNumber` 才拿到真值（39 页，首页 57 词）。

### Suggested Action
- 参数名与文案避免把估算写成事实（如 `--max-words` 就别叫"一页"）；
- 涉及"文档结构事实"（页数、条数）时必须找**权威来源**（Word COM / PDF 渲染），不能靠估；
- 拿到真值后**回填到文档与工具**（`--word-pages` / `--page N` 就是这么加的）。

### Metadata
- Source: user_feedback
- Related Files: `vocab-tool/_word_pages.ps1`, `vocab-tool/build_vocab.py`
- Pattern-Key: estimate.label_and_verify
- Recurrence-Count: 1
- First-Seen: 2026-10-14
- Last-Seen: 2026-10-14
- Tags: estimation, ground-truth, docx-pagination
