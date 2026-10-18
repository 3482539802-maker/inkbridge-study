---
name: inkbridge-study
description: 使用 InkBridge 配合华为笔记准备留白作答本、同步原始笔迹、核对手写证据并开展可追溯的课程辅导。用户提到 InkBridge、华为笔记做题记录、笔迹核图或接续该学习系统时使用；普通 HarmonyOS 开发不触发。
---

# InkBridge 学习助手

保留华为笔记作为书写应用。此 skill 是已安装 InkBridge 工具箱的跨工作空间入口，不包含用户课程、原始笔迹、签名或模型。

## 定位工具箱

以本 skill 所在目录为基准执行 `python -X utf8 scripts/bridge.py locate`，不要在当前工作空间猜测相对路径。解析返回的 `root` 后，所有项目脚本均使用该根目录的绝对路径，并将命令工作目录设为该根目录。

定位顺序：`INKBRIDGE_HOME` → `~/.codex/inkbridge/config.json` → skill 位于源码仓库时自动发现。缺失时仅要求用户提供已有克隆路径；也可按用户请求克隆公开仓库并运行 `scripts/install_skill.ps1`。用 `bridge.py configure --root <克隆目录>` 保存本机位置；配置不放进 skill 或 Git。

先读根目录 `AGENTS.md`、`docs/HANDOFF.md`、`docs/WORKFLOW.md`。深入实现时按 `docs/WORKSTREAMS.md` 选择对应模块。路径不存在表示该安装缺少对应组件，不要编造运行状态。

## 按请求推进

- **准备课程或题册**：读取 [references/workflows.md](references/workflows.md) 的材料流程，检查现有 CLI 的 `--help`，核对源页和切分，保留 hash/出处，作答本与电脑答案分离。不要将题目答案放入平板作答本。
- **继续学习或检查系统**：执行本 skill 的 `scripts/bridge.py status`，它只调用已有状态检查。结合新鲜 receiver/session 状态和实际进程判断是否在采集。需要设备检查时使用工具箱 `connect-tablet.ps1 -CheckOnly`；恢复连接使用 `-NoLaunch`。
- **核图、提示或批阅**：读取 [references/workflows.md](references/workflows.md) 的证据流程及工具箱 `docs/CHAT_RECOGNITION.md`（如有）。逐图核对文字、公式、图形和标注，保留不确定；通过已有队列/校验接口写入新版本，勿直接修改数据库或复活 superseded 任务。
- **开发或离线分析**：按工具箱模块交接执行。使用独立测试目录/端口，不启动第二套生产服务；源码更新不等于运行中代码已经加载。

## 操作与证据边界

用户亲自导入/导出 Notes，开始、授权、停止采集。不得自动启动或切换平板应用、点菜单、换页、滚动缩放、授权或显示悬浮层。活动采集期间不得停服务、重启或安装应用。不要自行续开已关闭的授权。

原始 `output/ink/sessions/` 不可改写、截断、删除；clock 与完整 history 一起保留。几何临时画面只用于获准的活动采集，处理后删除；停止后的截图不能补作书写时证据。素材、原始笔迹、课程记录、签名/密钥和浏览器状态默认只留本机，不上传公开或私有 Git。

坐标候选、外推、运动估计和 unknown 必须区分。工具选择/手势候选不是已确认的删除或移动，原始叠图不代表最终答案。接触时间不是思考时间，停顿/笔画数不是掌握度。分别报告来源、处理、识别和响应延迟，不将构建通过或约 240Hz 采样说成毫米精度或端到端实时。

识别和辅导由当前聊天接续，不承诺自动唤醒聊天、常驻本地 LLM 或未经配置的云 API。遇到证据不足时解释缺口，并继续处理已有证据支持的部分。
