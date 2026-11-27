# PET 词汇应用 · OCR 实施计划 v1（RapidOCR）

- 版本：v1.0（2026-11-27）｜状态：**已定案，可开工**
- 决策依据：`SPIKE_WindowsOCR_20261127.md`（Windows 内置 OCR 实测英文召回 50%、且依赖目标机语言包）
- 关联：`PRD_本期四功能_20261126.md` §6、`AGENTS.md`（硬规则与陷阱表）、`词库工作台_实施方案_v1_20261116.md`

---

## 0. 一句话决策

**用 RapidOCR（ONNX 推理，模型随包自带）做离线 OCR，作为「生成器上传图片」这一条支路；它不改变词库包结构，也不改变"播放阶段完全离线"这一前提。**

放弃 Windows 内置 OCR 的两个理由（实测，不再讨论）：

1. 本机 `en-US` / `en-GB` 均 `NOSUPPORT`，只能用中文模型认英文 → 英文召回 50%、`borrow`/`return` 整行丢失；
2. 它依赖**目标机器**的系统语言包，便携包无法携带 → 做不了"交付给别人"的功能。

## 1. 选型与证据（不是听说，是拆包看过的）

| 项 | 事实 | 来源 |
|---|---|---|
| 包名/版本 | `rapidocr==3.10.0`（旧名 `rapidocr-onnxruntime` 1.4.4 写死 `requires_python <3.13`，本机 3.13.14 装不上） | PyPI 元数据 |
| Python 兼容 | `>=3.8,<4` ✓ | 同上 |
| 模型是否随包 | **是**：wheel 内含 `models/PP-OCRv6_det_small.onnx`(9.5MB)、`PP-OCRv6_rec_small.onnx`(20.3MB)、`ch_ppocr_mobile_v2.0_cls_mobile.onnx`(0.6MB) | 拆 wheel |
| 推理引擎 | `rapidocr` **不自带** onnxruntime（`requires_dist` 里没有），需单独装 `onnxruntime` | 同上 |
| 默认引擎 | `config.yaml` 的 `EngineConfig.onnxruntime` 段，默认走 onnxruntime | 拆 wheel |
| 预处理 | 引擎内置 `max_side_len: 2000`、`min_side_len: 30`、纵向 padding —— **不用我们自己缩图** | `config.yaml` |
| 输出 | `RapidOCR()(img)` → `RapidOCROutput`，含 `boxes` / `txts` / `scores`（可开 `return_word_box` 拿字级框） | `rapidocr/main.py:93` |
| 许可 | RapidOCR Apache-2.0；PP-OCR 模型 Apache-2.0 | 上游 |

> 实装第一步（30 分钟）先跑 smoke：`RapidOCR()(png)` 并 assert `boxes/txts/scores` 三个字段存在、len 一致。**字段名不匹配就当场改，不按记忆写代码。**

## 2. 依赖与体积预算

新增依赖（全部钉死版本，写进 `requirements.txt`）：

```
rapidocr==3.10.0
onnxruntime==1.32.0
opencv-python-headless==5.1.0.95
numpy==2.5.5
Pillow==13.0.0
shapely==2.2.0
pyclipper==1.4.0
PyYAML==6.0.4
omegaconf==2.4.0
tqdm / requests / colorlog   （纯 Python，体积可忽略）
```

| 口径 | 数值 | 测量方式 |
|---|---|---|
| wheel 合计 | **≈111 MB** | PyPI 元数据逐个累加 |
| 装后预计 | **+170~200 MB** | 按 cv2/onnxruntime 解包膨胀估；**装完用 `Get-Size` 实测替换此估** |
| release zip | 85 MB → **约 260 MB** | 同上，发版时实测 |
| 首启耗时 | 预计 1~3 s（onnxruntime 会话初始化） | 装完实测；README 写明"第一次识别会慢几秒" |

> 体积是这次决策的主要代价。**发版前必须在 README/HOWTO 写清"包为什么变大"**，否则用户会以为装坏了。

## 3. 架构与数据流（守住 AGENTS §8 的分层）

```
上传         serve.py /api/upload（只做 HTTP；MAGIC 校验在 webgen.save_upload）
             └→ work/_uploads/<doc>
解析编排     webgen.start_parse_job（只编排，不 import OCR）
             └→ 子进程 build_vocab.py parse --in <img> [--keep-low]
                    └→ ocr_extract.py（新文件，纯函数，可被单测直接调用）
                           ├ RapidOCR 推理
                           ├ 行 + bbox → 按 x 聚类归列
                           ├ 列角色识别（英文列/中文列/音标列/序号列）
                           └ 按 y 配对 → [(en, cn, score)]
             └→ work/_parse/<doc>/words.csv（契约不变）
校对/生成     generator.html（不变）→ webgen.start_build_job（不变）
```

规则：

- **serve.py 不新增任何 OCR 逻辑**（只多一个 MAGIC 类型，且那也在 webgen 里）；
- **webgen.py 不 import rapidocr**（OCR 全在子进程里，进程退出即释放 ~200MB 内存）；
- `ocr_extract.py` 不 import `webgen`，可被 `_probe_ocr.py` 直接调用（与 `_probe_pack.py` 一个路子）。

## 4. 输入与预处理

| 项 | 规格 |
|---|---|
| 新格式 | `.jpg` `.jpeg` `.png`，加进 `webgen.MAGIC`：`\xFF\xD8\xFF` / `\x89PNG`（延续"按魔数不按扩展名"） |
| 上限 | 沿用 `MAX_UPLOAD = 20MB`，超限即拒（沿用现有六类拒绝的文案风格） |
| 一张图 | = 一个 doc = 一页。多页扫描件：分别上传，生成时用「并入」串起来（页号自动 offset） |
| EXIF | Pillow 读 `ImageOps.exif_transpose()` 纠正手机竖拍方向（否则整页躺倒，识别率归零） |
| 缩放 | **不做**：引擎内置 `max_side_len=2000` 已处理 |
| 扫描 PDF | **本期不做**（下一期：`fitz` 渲染空白页后走同一 OCR 链路） |

## 5. 抽取与列归并（这是本功能的核心算法）

1. **推理**：`engine(img)` → `boxes`（四点框）、`txts`、`scores`。
2. **行聚类**：按框的 y 中心排序；同一 y 带（容差 = 该行高度的 0.6 倍）合并为一行。
3. **列聚类**（一维）：取每个框的 x 左边界做排序，找最大间隔做切分；得到 2~5 个列带。
4. **列角色识别**（按列内文本构成打分，不按位置硬编码 —— 位置会因书而异）：

   | 角色 | 判据 |
   |---|---|
   | 序号列 | ≥80% 是纯数字 |
   | 音标列 | ≥60% 的 token 含 `[ ] / ː ə ʃ θ ð ŋ` 等 IPA 字符 |
   | 英文列 | 拉丁字母占比 ≥70%，且平均词长 ≥3 |
   | 中文列 | CJK 占比 ≥50% |

5. **配对**：同一 y 行里取「英文列」与「中文列」各一个；缺失任一侧 → 该行标记可疑并给出原因。
6. **清洗**：去掉行首序号（`^\s*\d{1,3}[.、]?\s+`）；中文去掉字间空格（`re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", s)`）；音标段剥离（沿用现有规则）。
7. **评分**：`score = min(det_score, rec_score)` 归一化；用于校对表"可疑"。
   - 「可疑」理由必须带**具体证据**：`OCR 置信度 0.62`、`未识别到中文列`、`英文列疑似音标`。
   - 空英文/空中文的行**不进 words.csv**，但要在 job 日志里记一条计数（不许静默 —— 这是 R6/§6 的底线）。

## 6. 与现有契约对齐（不许各写一套）

| 契约 | 本期复用方式 |
|---|---|
| `words.csv` 列结构 | 不变，OCR 只是另一种"解析器" |
| `score` 语义 | 不变（0~1，低于 `GROUP_LOW=0.9` 显示可疑） |
| `--keep-low` | 不变：网页永远带它，低分页不静默丢弃 |
| 「组」/「页」 | 一张图 = 1 页；**因为图片没有 Word 页码，走"组"还是"页"？→ 定为"页"但不带真实页码语义**：显示「第 1 张」。实现上 `with_pages=false`，文案在 `generator.html` 里按 `S.source_kind === 'image'` 分支 |
| 并入页号 offset | 不变（`offset = max_page`） |

> 最后一行需要一次文案决策：图片上传时 `describeGroups()` 要能说「第 N 张」而不是「第 N 组」。改动集中在 `generator.html` 的 `S.paged` 判定与两处文案，不动后端。

## 7. 校对联动（依赖 B「插一行」）

- OCR 必然有错：**B 必须先做**（PRD §5），否则用户只能整页重解析。
- 「可疑」徽标 + 行尾 `?` 理由（现有机制），理由文案见 §5.7。
- 上传卡文案：`docx / pdf / txt / jpg / png`，并给一条提示「照片请正对、四角完整、避免阴影；手写与竖排不支持」。

## 8. 便携包改造（`build_dist.ps1`）

1. `$Prod` 不变（不加 OCR 代码以外的文件）；`$pkgs` 追加：
   `rapidocr`、`onnxruntime`、`cv2`、`numpy`、`shapely`、`pyclipper`、`yaml`、`omegaconf`、`tqdm`、`requests`、`charset_normalizer`、`urllib3`、`idna`、`certifi`、`colorlog`、`antlr4`（omegaconf 依赖，按实际补）
2. `$keepMeta` 正则同步追加这些包的 `dist-info`（保留许可信息）。
3. **自检加一项**（现在的自检只 import 第三方库，漏 DLL 会全绿）：
   `& $rtPy -c "import rapidocr, onnxruntime, cv2; print('OCR-OK')"` **且真跑一次小图**，断言输出非空。
4. 发版后记录：`release` 目录与 zip 的实测体积，写进 README 的"包为什么这么大"。

## 9. 门禁与验收

| 门禁 | 新增内容 |
|---|---|
| `_probe_ocr.py`（新增） | 对 `_fixtures/ocr_page_22.png`（spike 那张合成页，22 条已知词条）跑 OCR，断言：英文召回 ≥90%、配对 ≥90%、`borrow`/`return` 不被丢；缺依赖时打 `SKIP` 并说明，不许假绿 |
| `_api_probe.py` | ① 真 jpg/png 魔数可上传 ② 伪扩展名（png 内容存成 .jpg 之外的情况）被拒 ③ >20MB 被拒 |
| `_e2e_generate.py` | 上传 png → 解析 → 校对表出现词条 → 生成 → 包里可播（用 3 条小图控时长） |
| `build_vocab.py doctor` | 加一行「OCR：rapidocr / onnxruntime 可导入，模型文件存在」 |

**及格线（承接 spike）**：`_probe_ocr.py` 的合成页必须 ≥90%；**真实照片另做一次 3 张的验收**（倾斜/阴影/正常各一），不达标则功能保留但 README 标注"照片质量要求较高"。

## 10. 任务分解（3–5 天）

| # | 任务 | 产出 | 工时 |
|---|---|---|---|
| 1 | 装依赖 + smoke（字段名/耗时/峰值内存） | 一页实测数据 | 0.5 天 |
| 2 | `ocr_extract.py`：推理 + 行聚类 + 列聚类 + 列角色 | 可对 png 输出 pairs | 1 天 |
| 3 | 接进 `build_vocab.py parse`（新 kind） + `webgen.MAGIC` | 端到端出 words.csv | 0.5 天 |
| 4 | `generator.html` 图片文案（「第 N 张」/提示） | UI 可读 | 0.25 天 |
| 5 | `_probe_ocr.py` + `_api_probe` / `_e2e` 用例 | 门禁可跑 | 0.75 天 |
| 6 | `build_dist.ps1` + `requirements.txt` + 自检 + 体积实测 | 便携包能带 OCR | 0.5 天 |
| 7 | 真实照片 3 张验收 + README/HOWTO 口径 | 验收记录 | 0.5 天 |
| 8 | 文档回写（AGENTS §5/§8、PRD、本计划状态改"已实现"） | 文档一致 | 0.25 天 |

## 11. 风险与回滚

| 风险 | 触发迹象 | 处置 |
|---|---|---|
| onnxruntime 在 3.13 上有 DLL/ABI 问题 | `import onnxruntime` 失败或崩溃 | 降到 1.22.x（有 cp313 轮子）再测；**不改 Python 版本** |
| 便携包体积超预期 | 装完 `Get-Size` > 250MB | 剪 cv2 的 `data/` 与 numpy 的 `tests/`；仍超则与用户重新确认是否降级为"仅开发机可用" |
| 首启慢被当成卡死 | 用户点"开始解析"后 3 秒无反应 | 生成器进度条先报「正在加载 OCR 模型…」，job 的 `phase=ocr` |
| 列聚类在真实照片上失效（歪斜/表格线） | `_probe_ocr` 通过但真实照片配对 <60% | 降级为"全部进校对表并标可疑"，**不自动猜配对**（R3） |
| 双列变单列（书是"英中混排一行"） | 只检测到 1 个列带 | 用 `en` 与 CJK 的正则切分同一行，按"英在前、中在后"配对；仍不确定就标可疑 |
| 许可 | — | Apache-2.0，LICENSE 里补一行来源说明 |

**回滚**：OCR 是**新增支路**，删掉 `ocr_extract.py` + MAGIC 两项 + 依赖即可回到今天的状态；词库包与已有页不受影响。

## 12. 本期不做 / 下一期

- 扫描 PDF（`fitz` 渲页后走同一链路）
- 多图合成一个 doc（现在靠「并入」）
- 手写体、竖排、表格跨页
- 字级框（`return_word_box`）—— 先不用，列级够
