# Loci · 本地 LLM 接入说明（Qwen2.5-3B）

**LLM 做首轮判定**：案件识别后（非手工预填时），让本地模型**读内容、理解材料性质**，从"本案真实文件夹清单"里选目标并给文书名。
关键词规则降为**兜底**（LLM 未启用 / Ollama 不可用 / 置信度过低时才用）。
原因：关键词只是字面命中、读不懂上下文（例：鉴定申请书里的"委托书"指司法委托，会被误判成当事人授权委托书）；LLM 读全文能分清。
**全本地离线**，Ollama 不可用时自动回退关键词，不影响主流程。

归档优先级：`人工预填 > LLM 首轮判定 > 关键词规则兜底 > _待分类（进待办复核）`

> **状态（2026-06-23）：已安装 qwen2.5:3b 并启用（`llm.enabled: true`），实测可用、~0.5s/份。**
> 集成时已内置三处稳健处理（你不用管）：① 结构化输出 JSON Schema（避免小模型残缺 JSON/乱码）；
> ② 输出长度上限 num_predict（避免"跑飞"超时）；③ 清单用案件磁盘真实文件夹 + 去斜杠容错匹配。
>
> **想更准**：3B 偶尔会"自信地分错"（低置信/错的仍会进待办复核）。把 `config.yaml` 的 `model` 改成
> `qwen2.5:7b`（先 `ollama pull qwen2.5:7b`，~4.7GB）即可，代码无需改动。

## 启用步骤（都在本机，无需 brew）

1. **装 Ollama**：到 https://ollama.com 下载 macOS 版（.dmg），装好后它在后台跑（端口 11434）。
2. **拉模型**：终端执行
   ```
   ollama pull qwen2.5:3b
   ```
   （约 2GB；想更轻可换 `qwen2.5:1.5b`）
3. **打开开关**：编辑 `auto_filer/config.yaml` 的 `llm` 段：
   ```yaml
   llm:
     enabled: true            # ← 改成 true
     model: qwen2.5:3b        # 想换模型改这里即可
     host: http://localhost:11434
     min_confidence: 0.6      # 低于此值不采用，仍进待办复核
     max_chars: 2000          # 喂给模型的正文字数
     timeout: 30
   ```
4. **试运行验证**：先用 dry-run 看效果，确认无误再正式跑
   ```
   cd auto_filer && python3 auto_filer.py --once --dry-run -v
   ```
   日志里出现 `🤖 LLM 归类: …（conf 0.xx）` 即说明 LLM 生效。

## 工作机制

- **触发条件**：案件已识别 + 非手工预填 + 有可读内容 → 先调 LLM。手工预填了文件夹时**不调用**（人工最高优先）。
- **枚举约束**：模型只能从本案场景内的文件夹清单里选，选不中清单的结果一律丢弃，不会乱建文件夹。
- **置信度**：LLM 返回 confidence；≥ `min_confidence` 才采用，否则照旧进 `_待分类` → 待办复核。
- **命名**：采用 LLM 时，用它给的文书名（如"鉴定材料"），仍套标准格式 `YYMMDD 简称 主体 文书名 版本`。

## 关闭 / 切换

- 关闭：`enabled: false`（或 Ollama 不开机，自动降级）。
- 换模型：改 `model`（如 `qwen2.5:1.5b` / 自建的 `minicpm`），代码不用动。

## 相关文件

- 模块：`auto_filer/llm_classify.py`（Ollama 调用 + 枚举校验）
- 接入点：`auto_filer/auto_filer.py` 的 `process_file`（match_rule 之后、fallback 之前）
- 配置：`auto_filer/config.yaml` 的 `llm` 段
