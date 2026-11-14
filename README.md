# Smartbook Author Skill

面向智慧教材作者及其 AI 编程助手的智慧书制作技能。智慧书是**同一本教材的交互式 HTML 表现形式**：作者在现有教材下上传包含完整章节的智慧书，与 PDF 电子书并行管理。

技能引导助手核对教材章节、设计交互与动画、进行浏览器验收，并将 `index.html` 及本地资源打包为 ZIP。仓库包含 Python 3 标准库打包和静态检查工具；平台执行确定性的格式与安全检查后，由管理员人工审核，发布状态以平台实际结果为准。

## 安装

先取得本仓库，例如：

```bash
git clone https://github.com/Deddyp1g/smartbook-author-skill.git
```

然后将 `skills/smartbook-author` **整个目录**复制到所用工具的技能目录。以下路径是相对于项目根目录的项目级安装；个人安装可使用对应的用户目录。使用文件管理器复制也可以。

| 工具 | 项目级目录 | 个人目录 | 支持依据 |
| --- | --- | --- | --- |
| Codex | `.agents/skills/smartbook-author` | `~/.agents/skills/smartbook-author` | [OpenAI 文档](https://developers.openai.com/codex/skills.md) |
| Claude Code | `.claude/skills/smartbook-author` | `~/.claude/skills/smartbook-author` | [Anthropic 文档](https://code.claude.com/docs/en/skills) |
| DeepSeek Harness (DSH) | `.dsh/skills/smartbook-author` 或 `.agents/skills/smartbook-author` | `~/.dsh/skills/smartbook-author` 或 `~/.agents/skills/smartbook-author` | [DSH 文件系统技能说明](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/skill/skill-filesystem/README.md) |
| CodeArts Snap | 参见下文 `CODEARTS.md` 适配入口 | 未核实 | [华为官方示例](https://github.com/huaweicloud-samples/modelarts-vllm-ascend-deploy-skill) |

例如，在 Linux/macOS 的项目目录内执行：

```bash
mkdir -p .agents/skills
cp -R /path/to/smartbook-author-skill/skills/smartbook-author .agents/skills/
```

Windows PowerShell（首次安装，目标技能目录尚不存在时）：

```powershell
New-Item -ItemType Directory -Force .agents/skills | Out-Null
Copy-Item -Recurse 'C:\path\to\smartbook-author-skill\skills\smartbook-author' '.agents\skills\smartbook-author'
```

Claude Code 请将以上目标目录中的 `.agents` 换成 `.claude`。DSH 可使用 `.agents` 或 `.dsh`；技能目录应直接包含 `SKILL.md`，不要多嵌套一级。更新已安装的技能前，请检查并备份其中的本地修改，然后替换原有技能目录，避免复制出嵌套目录。安装后新开会话，检查工具的技能列表，并要求助手使用 `smartbook-author`；不同版本的发现行为可能不同。

华为资料表明 CodeArts Snap 的工作区可以使用 `CODEARTS.md` 作为指令入口，并未证实所有版本原生安装 `SKILL.md`。将仓库的 [CODEARTS.md](CODEARTS.md) 内容合并到项目已有的同名文件，并按照该文件放置技能目录；不要覆盖已有指令。具体 Snap 版本仍需实测。

## 使用

向助手提供教材 PDF 或作者拥有权利的原始内容、目标读者、期望交互以及智慧教材平台的当前上传要求。例如：

> 使用 smartbook-author，为这本教材制作覆盖全部章节的智慧书。保留原书结构，设计交互实验与动画，完成桌面和手机浏览器检查，并输出可上传的 ZIP。

技能中的 [格式说明](skills/smartbook-author/references/package.md) 和工具提供本地准备流程。示例调用：

```bash
python skills/smartbook-author/scripts/smartbook.py pack /path/to/dist /path/to/smartbook.zip
python skills/smartbook-author/scripts/smartbook.py validate /path/to/smartbook.zip
```

以上命令从本仓库根目录运行；如果安装在别处，请将脚本路径改为已安装技能中的路径。脚本只做有限的本地静态检查，不能证明网页安全、章节完整或一定通过平台上传；不能替代平台的确定性检查和管理员人工审核。平台的限制、审核结果和实际发布流程以部署版本为准。需有 Python 3.9 或更新版本；脚本无需安装额外依赖。

当前智慧书阅读器未启用存储桥，原生 `localStorage` 也可能不可用；智慧书应在不能保存主题和阅读位置时仍可正常使用。随技能保留的存储客户端仅供未来支持该协议时实验，不能作为当前阅读器的持久化方案；详见 [存储说明](skills/smartbook-author/references/storage.md)。

开发者可在仓库根目录运行 `python -m unittest discover -s tests -v` 和 `node --test tests/test_storage.cjs` 验证打包工具和存储客户端；后者需要 Node.js。

## 兼容性与许可

上述安装目录来自各工具公开资料。已在隔离项目中用 Codex CLI `0.167.0-alpha.4.1` 验证项目技能发现：`skills/list` 返回该技能已启用且无加载错误，尚未用模型验证完整制作流程。Claude Code `2.1.201` 已检测到安装，但没有完成技能加载实测；DSH 和 CodeArts Snap 尚未在本机实测。本仓库尚未证明所有版本的工具都能自动加载，也未证明所有平台版本接受生成的包。请在目标版本中检查技能发现、显式调用和实际上传。DSH 官方项目处于开发预览阶段，接口可能变化。CodeArts Snap 的 `CODEARTS.md` 入口基于华为官方示例，尚无可核实的完整格式与版本规范。

代码和技能说明采用 [MIT 许可证](LICENSE)。该许可证不授予对作者提供的教材、图片或其他第三方素材的使用权。
