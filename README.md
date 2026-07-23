# 涉外诉讼案件工作区

> 一键部署 | 前台收件箱 | 自动 OCR 归档 | 纯本地运行  
> 适用: 中伦律师事务所 · 涉外诉讼 · 产品质量纠纷

---

## 快速开始

```bash
# 1. 解压后运行（仅第一次）
bash setup.sh

# 2. 新建一个案件
bash bin/new_case.sh "2026-甲公司-产品责任纠纷"

# 3. 编辑 auto_filer/config.yaml，在 cases 下添加案件关键词

# 4. 启用自动归档（含智能命名）
bash bin/filer.sh start

# 5. 往 inbox 丢文件 → 自动归位
```

---

## 工作流

```
                         ┌──────────────────────┐
                         │    收件箱 inbox/      │  ← 微信/邮件/扫描件丢这里
                         │  (Finder 快捷访问)     │
                         └──────────┬───────────┘
                                    │
                          ┌─────────▼──────────┐
                          │   auto_filer.py     │
                          │                     │
                          │  docx → 读内容       │
                          │  PDF  → pdftotext   │
                          │  扫描PDF → OCR       │
                          │  照片   → OCR        │
                          │  xlsx  → 读单元格    │
                          │                     │
                          │  匹配案件 + 归类规则  │
                          │  重命名标准格式       │
                          └─────────┬──────────┘
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │   cases/ 案件文件夹   │
                         │                      │
                         │  案件A/04_诉讼文书/    │
                         │      起诉状/过程稿/     │
                         │   2026-06-22-起诉状.docx│
                         └──────────────────────┘
```

---

## 目录结构

```
case_workspace/                  ← 工作区根目录
├── setup.sh                     ← 一键部署脚本
├── quick_start.sh               ← 快速启动
├── .env                         ← 工作区配置
├── inbox/                       ← 收件箱（文件丢这里）
├── cases/                       ← 案件文件夹
├── logs/                        ← 操作日志
├── bin/
│   ├── new_case.sh              ← 新建案件
│   └── filer.sh                 ← 归档控制 (start/stop/once/dry-run)
├── auto_filer/
│   ├── auto_filer.py            ← 核心引擎
│   └── config.yaml              ← 案件列表 + 归类规则
└── config/
    └── com.zl.autofiler.plist   ← macOS launchd 服务
```

---

## 命令速查

| 命令 | 用途 |
|------|------|
| `bash bin/new_case.sh "案件名"` | 创建新案件文件夹模板 |
| `bash bin/filer.sh start` | 启动后台守护（自动归位） |
| `bash bin/filer.sh stop` | 停止守护 |
| `bash bin/filer.sh once` | 单次处理收件箱 |
| `bash bin/filer.sh dry-run` | 试运行（看归类效果） |
| `bash bin/filer.sh status` | 查看状态 |
| `bash bin/filer.sh log` | 最近日志 |
| `bash bin/filer.sh inbox` | 打开收件箱 (macOS Finder) |
| `bash bin/filer.sh dashboard` | 🌐 启动可视化控制台 (http://localhost:5080) |

---

## 可视化控制台

```bash
bash bin/filer.sh dashboard
# → 浏览器打开 http://localhost:5080
```

五个标签页：

| 标签 | 功能 |
|:--|:--|
| 📊 总览 | 守护状态 + 收件箱计数 + 案件数 + 快捷扫描按钮 |
| 📬 收件箱 | 拖拽上传文件 + 待处理文件列表 + 一键归档 |
| 📁 案件浏览 | 案件卡片 → 点击展开文件夹树 |
| 📋 归档日志 | 实时操作记录（最近100条） |
| 🔍 搜索 | 跨案件文件名搜索 |

纯本地运行，不上传任何数据。关掉终端即停止。

---

## 添加新案件

编辑 `auto_filer/config.yaml`：

```yaml
cases:
  - key: "NewCase"
    name: "2026-新客户-案由"
    keywords: ["新客户", "NewClient", "产品型号", "案号"]
```

然后运行 `bash bin/new_case.sh "2026-新客户-案由"` 创建文件夹。

---

## 智能文件命名

文件归档时自动从内容提取信息，拼成标准名称：

**格式**: `[文档日期]-[案件简称]-[(案号)]-[文书类型]-[版本号].[扩展名]`

| 原始文件名 | → 智能命名 | 提取来源 |
|:--|:--|:--|
| `新文档_1.docx` | `2025-03-15-甲方-(00001)-民事起诉状.docx` | 内容: 2025年3月15日 / X0000民初00001号 / 标题"民事起诉状" |
| `Document_v0.2.docx` | `2026-06-22-甲方-质证意见-v0.2.docx` | 内容: 标题"质证意见" / 原文件名: v0.2 |
| `乙方答辩状-终稿.docx` | `2026-06-22-乙方-民事答辩状.docx` | 内容标题比原名更精确 |
| `IMG_5921.jpg` | `2026-06-22-甲方-IMG.jpg` | OCR 模糊时降级保留关键词 |

**各字段提取优先级**:

| 字段 | 优先级1 | 优先级2 | 优先级3（兜底） |
|:--|:--|:--|:--|
| 日期 | 内容中 `2025年3月15日` | 内容中 `2025-03-15` | 当前日期 |
| 案号 | `(2025)X0000民初00001号` → `00001` | `00001号` | 不填 |
| 文书类型 | 内容标题（docx首个标题） | 规则 rename_pattern | 原文件名关键词 |
| 版本号 | 原文件名 `v0.2` | — | 不填 |
| 案件简称 | config.yaml case key | — | — |

**切换模式**（`auto_filer/config.yaml`）：

```yaml
naming:
  mode: "smart"       # smart=智能提取 | simple=日期+类型 | keep=保留原名
```

---

## 添加新归类规则

编辑 `auto_filer/config.yaml` 的 `rules` 段：

```yaml
rules:
  - keywords: ["新文书类型", "document type"]
    target: "04_诉讼文书/新目录/过程稿"
    rename_pattern: "新文书"
```

---

## OCR 能力

| 文件 | 识别方式 | 效果 |
|:--|:--|:--:|
| `IMG_4823.jpg` (手机拍照) | Tesseract OCR (chi_sim+eng) | ✅ |
| 扫描版 PDF (法院来文) | pdftoppm → OCR | ✅ |
| 微信压缩图 | OCR | ⚠️ 取决于清晰度 |

---

## 系统要求

- **Python**: 3.10+
- **系统工具**: `tesseract-ocr` (含中文包 chi_sim) + `poppler-utils`
- **Python 包**: watchdog, pyyaml, python-docx, openpyxl, pytesseract, Pillow
- **macOS**: 推荐配 launchd 开机自启

---

*不上传任何文件到 AI/云端。纯本地运行。*
