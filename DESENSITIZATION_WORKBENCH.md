# 脱敏工作台更新

更新日期：2026-11-25

## 本分支内容

同步本地已实现的脱敏工作台及运行所需的共享模块。复用 Flask + pywebview 宿主界面，未将工作台拆为独立应用。无需云端大模型；本次上传不包含本机真实案件配置、材料、日志、化名映射或旧本地 Git 历史。

### 主要变化

- 文件选择和范围设置放入可收起的右侧面板；原文与预览分别滚动。
- 首页移除宣传区，直接呈现功能与状态。
- 支持案件内材料和访达独立选文件两种入口。
- 自定义范围按组选择，类别标签完整显示。
- 划选文字补标后，点击“按补标匹配全文”批量标记当前文件的相同文字。
- 匹配支持中文断行、全角字符、英文大小写；处理重复、重叠、单词边界。
- 保留已排除的误标，支持撤销；同一文件重新识别保留手工修订，换文件清空关键词。
- 更改标记后，旧预览失效；重新生成并复核后保存副本。
- 校正浏览器 UTF-16 和 Python 字符偏移，避免补充字符造成脱敏错位。

## 启动

建议 Python 3.11；Apple Silicon Mac 使用原生 ARM64 Python。

```bash
bash setup.sh
bash launch.command
```

安装需要下载依赖，日常处理在本机完成。安装脚本根据示例生成本机配置，示例案件列表为空。请通过启动入口打开，静态 HTML 文件不能独立提供脱敏服务。

## 工作流程与当前范围

选择案件材料或独立文件 → 识别 → 手动补标 → 按补标匹配全文 → 复核 → 生成脱敏 → 选择工作区并保存。

关键词仅用于当前文件会话，未作为跨文件词库持久保存，不自动推断简称。当前工作台保存的是文本/Markdown 脱敏副本，并不是已经清除原 PDF 页面、附件和元数据中敏感信息的原格式文件。原格式处理代码及其独立测试位于 `loci/desensitize/`。

## 关键文件

| 路径 | 用途 |
| --- | --- |
| `loci/run.py` | 本机服务与桌面窗口 |
| `loci/app.py` | 文件选择、识别、预览及输出接口 |
| `loci/templates/index.html` | 工作台页面 |
| `loci/static/app.js` | 手工复核与状态控制 |
| `loci/static/desk_terms.js` | 关键词匹配 |
| `loci/desensitize/` | 规则、OCR、格式处理和化名 |
| `auto_filer/`、`bin/` | 宿主界面运行依赖及空案件示例配置 |

## 测试

```bash
node --test tests/test_desk_terms.cjs
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python -m unittest loci.desensitize.test_normalization loci.desensitize.test_format_io
```

另附浏览器交互回归 `tests/test_desk_workbench.cjs`，需要另行安装 Node.js Playwright 与 Chromium。其使用虚构材料，拦截页面业务请求，通过本机 Python 测试客户端执行实际脱敏替换接口。

测试中的人物、号码和 `example.invalid` 邮箱为合成数据，不代表真实案件。
