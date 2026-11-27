# VocabSpeaker · 外语词汇听读

把一本教材的词汇表（`docx` / `pdf` / `txt`）变成**可以循环听读的离线词库包**：
每个词一条 MP3，按页一页页往下听，点任意词条重听，播放阶段完全不用联网。

---

## 下载（不想配环境就看这一节）

**[⬇ 下载 Windows 便携包（最新版）](https://github.com/outdatecoder/vocabspeaker/releases/latest)** —— 约 85 MB，Windows 10/11 64 位。

> 也可以从仓库右侧 **Releases** 进入，选最新版本里的 `vocabspeaker-win64-<日期>.zip`。

三步就能用：

1. **解压** zip（右键「全部解压缩」；不要在压缩包里直接双击）
2. 进入解压出来的 `vocabspeaker-win64\` 文件夹，双击 **`vocab-tool\start_pet.bat`**
3. 浏览器会自动打开 `http://127.0.0.1:8901/`，看到「PET主题词汇」「初中英语词汇表」两张卡片就成了

**只想听、不打算生成新词库？** 连 bat 都不用开：解压后直接双击
`vocab-tool\packs\初中英语词汇表\index.html`，离线就能播。

便携包**自带 Python 3.13 运行时**（`runtime\`）、PDF 解析（PyMuPDF 1.28.2）与 FFmpeg 7.1，
目标机器不需要装 Python、Office、ffmpeg。生成新词库要联网（edge-tts 是云端服务），播放完全离线。

想自己改代码：`git clone` 本仓库 → `pip install -r vocab-tool/requirements.txt` → 双击同一个 bat。

- 生成的词库在 `vocab-tool\packs\<词库名>\`，双击里面的 `index.html` 就能听，**不联网**。
- 合成新音频要联网（edge-tts 是云端服务），已生成过的词命中 `vocab-tool\cache\`，不会重复调用。
- 播放器模板 `pet-player-sketch\index.html` 是必需的，`vocab-tool` 这个目录名不能改。
- 包内 `LICENSE` 记录第三方许可（PyMuPDF 为 AGPL、FFmpeg 为 LGPL/GPL），**转发给别人时别删**。

**只需要转发 `release\vocabspeaker-win64\` 这一个文件夹。** 仓库根的其他东西
（`.gitignore`、`build_dist.ps1`、两份源码副本）都是给开发和发版用的，对方用不上：

| 文件夹/文件 | 对方删掉会怎样 |
|---|---|
| `runtime\` 180 MB | 没有 Python，`start_pet.bat` 会去找 PATH 里的 python |
| `vocab-tool\` 26.7 MB | 没有网页界面，只剩词库里的 `index.html` 能听 |
| `pet-player-sketch\` 0.2 MB | 已有词库能听，但**生成新词库会失败**（渲染模板缺失） |
| `vocab-tool\packs\` | 词库没了；不想带可以删（对方自己生成） |
| `vocab-tool\cache\` 8 MB | 不影响播放；只是对方重新生成同一批词时要联网 |
| `HOWTO.txt`、`LICENSE` | 纯文档，建议留着 |

> 本目录就是 GitHub 仓库根：`release/`（便携包，已在 `.gitignore` 里）只用于上传 Releases。

生成的产物长这样：

```
vocab-tool/packs/<词库名>/
├── index.html            ← 用户双击这一个
├── pack_data.json
└── audio/
    ├── p001_001.mp3 …    ← 每个词一个文件（播放就播它，不做时间轴 seek）
    └── p001.mp3          ← 整页 MP3，仅供「下载本页 MP3」
```

---

## 两种用法，需要的东西完全不同

| 你想做的事 | 需要安装什么 |
|---|---|
| **只想听**已有的词库 | **什么都不用装**。双击 `packs/<词库>/index.html` 即可 |
| **想生成**新词库 | Python 3.13 + 4 个依赖（见 `vocab-tool/requirements.txt`） |

### 只想听：把整个文件夹拷给别人

`packs/<词库>/` 是自包含的：`index.html` + `audio/*.mp3`，音频路径全是相对路径。
整个文件夹打成 zip 发给别人，对方解压后双击 `index.html` 就能播 —— 不联网、不装 Python、不用起服务。

---

## 生成新词库

```powershell
cd vocab-tool
pip install -r requirements.txt
```

> `edge-tts` 必须 **>= 7.3.1**：更早的版本用 v1 令牌，国内出口会被微软拒绝（403）。
> 清华镜像没有 7.3.x，需要走官方 PyPI。

然后双击 `vocab-tool/start_pet.bat`，浏览器会打开 `http://127.0.0.1:8901/`：

1. **＋ 新建词表** → 选文件 → （docx 可选「按 Word 真实页码分页」）→ **开始解析**
2. 在校对表里改词、删掉没用的行（折叠即释放 DOM，几千词也不卡）
3. 选目标词库 → **开始生成**，进度条旁会给出预计完成时间

`start_pet.bat` 按这个顺序找解释器：

| 顺序 | 位置 | 用途 |
|---|---|---|
| 1 | `../runtime/python.exe` | 便携发布包自带 |
| 2 | `vocab-tool/local_python.txt` 里的路径 | 开发机指定解释器 |
| 3 | PATH 里的 `python` | 系统已装 Python |

没有便携包又不想改 PATH，就新建 `vocab-tool/local_python.txt`，内容写一行你的 `python.exe` 绝对路径。

---

## 目录结构与两个硬约束

```
<任意父目录>/
├── vocab-tool/                ← 名字不能改
│   ├── serve.py               ← HTTP 层（路由 / token / Range 静态）
│   ├── webgen.py              ← 编排层（上传 / 解析 / 分组 / stage / 锁 / 取消）
│   ├── build_vocab.py         ← 解析与合成（子进程入口，也可单独命令行用）
│   ├── home.html              ← 工作台（/）
│   ├── generator.html         ← 生成器（/generator）
│   ├── start_pet.bat
│   ├── requirements.txt
│   ├── packs/                 ← 生成的词库（不进 git）
│   └── work/                  ← 上传原件 / 中间产物 / 回收站（不进 git）
└── pet-player-sketch/
    └── index.html             ← 播放器渲染模板（必需）
```

**硬约束 1：`vocab-tool` 这个目录名不能改。** `webgen.py` 里的词库链接写死为
`/vocab-tool/packs/<id>/index.html`，改名后卡片全部 404。

**硬约束 2：`vocab-tool` 必须待在某个父目录下**，不能把里面的文件直接摊到磁盘根目录。
`serve.py` 的静态根（docroot）是脚本的**上上级**。

---

## 需要联网的地方

- **生成**：`edge-tts` 是云端服务，所以合成要联网。命中词级缓存（`vocab-tool/cache/`）的词不需要联网。
- **播放**：完全离线。
- `ffmpeg` 不需要单独装 —— `imageio-ffmpeg` 自带二进制。

---

## 已知限制

- 「按 Word 真实页码分页」需要本机装 Office（走 Word COM）。不勾选时按固定 50 词一组，不需要 Office。
- 分组与页是两回事：没勾真实页码时界面上叫「组」，写进包之后才叫「页」。**同一本书分批并入时，单位必须一致**，否则边界对不上会出现重复词。
- 词库是**按目录名**标识的；改名要删掉首位的 `_`/结尾的点（Windows 建不出这种目录）。

---

## 这个仓库里有什么 / 没什么

```
vocabspeaker/                 ← 仓库根
├── README.md  LICENSE  HOWTO.txt
├── build_dist.ps1            ← 打包脚本（生成便携包）
├── vocab-tool/               ← 目录名不能改
│   ├── serve.py  webgen.py  build_vocab.py
│   ├── home.html  generator.html  start_pet.bat  requirements.txt
│   ├── _word_pages.ps1       ← Word 真实页码扫描（缺失则「按页分页」直接报 400）
│   ├── packs/                ← 生成的词库（不进 git）
│   └── work/  cache/         ← 运行期数据（不进 git）
├── pet-player-sketch/index.html
└── release/vocabspeaker-win64/   ← 便携包成品，上传 Releases（不进 git）
```

包含：`vocab-tool/` 的运行必需文件、播放器模板、`README.md`、`LICENSE`、`.gitignore`、`build_dist.ps1`、`HOWTO.txt`。

**不包含**（`.gitignore` 已排除）：

| 内容 | 原因 |
|---|---|
| `packs/`（词库与音频） | 大二进制，走 GitHub Releases |
| `work/` `cache/` | 含别人上传的原始文档、删除备份、词级缓存 |
| `release/`、`runtime/`、`dist/` | 便携包成品 ~207 MB，走 GitHub Releases |
| `_edgeprof*/` | Edge 调试 profile，含 cookie 与浏览记录 |
| 开发门禁脚本（`_cdp_*.py`、`_api_probe.py`、`_e2e_generate.py` 等） | 依赖本机绝对路径与额外包，尚未整理 |

## 打包 / 发版

```powershell
# 目标：生成 release\vocabspeaker-win64\，连同现有词库与词级缓存一起带走，不压缩
powershell -NoProfile -ExecutionPolicy Bypass -File build_dist.ps1 `
    -Out "release" -Name "vocabspeaker-win64" `
    -WithPacks -WithCache -NoZip `
    -Py "C:\path\to\python313\python.exe"
```

产物 = 源码 + 便携 Python 运行时（`runtime\`）+ PyMuPDF（PDF 支持）+ 官方 FFmpeg 7.1 + `packs\` + `cache\`。
对方解压后双击 `vocab-tool\start_pet.bat` 即可。

| 开关 | 作用 |
|---|---|
| `-Out <目录>` | 输出到哪（默认 `dist`） |
| `-Name <名字>` | 文件夹与 zip 的名字（默认 `pet-vocab`） |
| `-WithPacks` | 带上现有词库 `vocab-tool\packs` |
| `-WithCache` | 带上词级 TTS 缓存 `vocab-tool\cache`（离线重生成同页不联网） |
| `-NoZip` | 只出文件夹，不压缩 |
| `-NoRuntime` | 只出源码（对方自己有 Python） |

脚本在出包前会自检，任一失败就拒绝交付：依赖能否导入、**`sys.path` 每一条是否都在 `runtime\` 内**
（防止用到目标机器自己的 site-packages）、ffmpeg 是否可用、`serve.py --help` 能否启动。

发版步骤：

1. 跑上面的命令，得到 `release\vocabspeaker-win64\`；
2. 先在包里跑过一次（会生成 `vocab-tool\__pycache__\` 与 `work\_*` 空目录），
   压 zip 前把 `__pycache__` 删掉，否则本地路径与编译缓存会跟着发出去；
3. 压缩成 `vocabspeaker-win64-<日期>.zip`（Releases 上传用 zip，仓库里不留）；
4. GitHub → Releases → Draft a new release → tag `v<版本>` → 上传 zip → 发布；
5. zip 里含第三方二进制（Python / PyMuPDF / FFmpeg），许可见 `LICENSE` 末节。

---

## 许可

代码以Apache-2.0 许可发布（见仓库根目录 `LICENSE`）。

打包给别人的 zip 里含第三方二进制，各自许可见其项目主页：

- **FFmpeg**（随 `imageio-ffmpeg` 分发）— <https://ffmpeg.org/legal.html>，LGPL/GPL
- **PyMuPDF / MuPDF** — AGPL-3.0（仅用于 PDF 词表抽取）
- **edge-tts** — 调用微软 Edge 的朗读服务，仅作个人学习用途
