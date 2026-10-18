# InkBridge Study

让华为笔记继续负责书写，让电脑负责留白题册、原始笔迹证据与可追溯学习辅导。
包含 HarmonyOS 采集端、Windows/Python 工具箱，以及可在其他工作空间调用的
**`inkbridge-study` skill**。

## 安装 skill

先安装 Python 3.11+、PowerShell 7 和 Git。Windows 的无空格路径示例：

```powershell
git clone https://github.com/3482539802-maker/inkbridge-study.git C:/InkBridge
Set-Location C:/InkBridge
python -m pip install -r requirements.txt -r requirements-visual.txt -r requirements-materials.txt
./scripts/install_skill.ps1
```

安装器将 skill 复制到用户级 `$CODEX_HOME/skills`（默认 `~/.codex/skills`），
仅将工具箱位置保存在本机 `~/.codex/inkbridge/config.json`。安装后在新聊天中使用：

更新安装默认保留已经配置的工具箱位置；需要主动切换时使用 `./scripts/install_skill.ps1 -SetDefault`。
状态检查有超时与新鲜度门槛，失败表示健康未知，须检查已有服务，不能据此重复启动。

> 使用 $inkbridge-study，检查已有学习系统，并帮我把这份试卷做成留白作答本。

也可以自然提到“用 InkBridge 核对我的笔迹并给渐进提示”。
skill 负责定位工具箱、调用现有流程和遵守证据边界；它不自带用户课程、模型、
签名或已经配置好的平板。平板采集还需按[运行指南](docs/RUNBOOK.md)自行构建、签名和授权。

## 能力与限制

- 源题裁切、hash/出处、宽留白 PDF 与 PDF 背景 `.hinote`，电脑详解与平板题册分离。
- 原始笔/手指事件持久化、ACK、断点回补与时钟历史。
- 原件配准、逐笔页面坐标候选、外推及明确 unknown 原因。
- 不可变课程绑定、当前聊天多区域核图、渐进反馈、本地知识库和网页补强提交。

原型曾在 HarmonyOS API 24 平板实测，但需要相应权限与用户屏幕采集授权，
不保证其他固件通用。没有毫米精度、最终擦改对象完整性或全天可靠性的独立证明。
原始叠图不代表最终答案，接触时间不代表思考时间，停顿不代表掌握度。
当前聊天需要用户接续，不会自动唤醒；默认不配置外部推理 API。

## 隐私与验证

公开仓库使用全新历史，只分发源码、通用文档和干净模板。
真实笔迹、试卷/教材、学习记录、设备端点、签名、模型与本机配置不随仓库发布。
详见[隐私边界](docs/PRIVACY.md)。

```powershell
python -X utf8 scripts/verify_workspace.py --group core
python -X utf8 scripts/verify_pc_pipeline.py
python -X utf8 scripts/verify_session_replay.py
```

依赖私有实测材料的测试会明确 skip；这不构成真机验证。
可选模型测试需另配已有 torch 环境。ArkTS/native 更改还需运行 `./build-harmony.ps1`。

[流程](docs/WORKFLOW.md) · [运行](docs/RUNBOOK.md) · [架构](docs/ARCHITECTURE.md) ·
[开发分块](docs/WORKSTREAMS.md) · [MIT](LICENSE) · [第三方许可](THIRD_PARTY_NOTICES.md)

本项目与华为及 OpenAI 无隶属关系。
