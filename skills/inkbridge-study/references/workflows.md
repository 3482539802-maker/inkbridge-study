# 工具箱流程

以下路径均相对于 `bridge.py locate` 返回的工具箱根目录，不能相对于发起请求的工作空间执行。先读现有文档及脚本 `--help`，不猜参数。

## 材料与留白题册

1. 使用用户指定的本地源文件；另存新产物，不覆写原件。`scripts/course_workbook.py` 处理题目计划、裁切、PDF/manifest；`scripts/hinote_workbook.py` 封装 PDF 背景原生题册。相关依赖在 `requirements-notes.txt`。
2. 核对题干、图形、题号、裁切边界和作答留白，保留源文件 SHA256、页/区域、题目 ID。复杂材料需要逐页视觉检查，不能只信 OCR。
3. `scripts/course_solution_guide.py` 的详解只给电脑使用；平板题册保持无答案。
4. 传入平板公共 Docs 目录使用 `scripts/tablet_files.py`；导入由用户执行。生成 UUID 不代表 Notes 导入后的真实 UUID。按实际曝光/导出证据建立不可变绑定。

## 原始采集与状态

先检查 `start-study.ps1 -Status` 与新鲜会话状态；只在用户请求启动且确认无已有实例时使用 `start-study.ps1`。本机配置通常在忽略的 `output/learning/runtime.json`，模板在 `config/study-workflow.example.json`。不要用模板覆盖现有配置。

检查设备用 `connect-tablet.ps1 -CheckOnly`，恢复路由用 `-NoLaunch`；记忆连接信息在忽略的 `output/device-connection.json`。状态检查失败表示未知/不可达，不能当作服务不存在而重复启动。不可为日常检查启动观察器、截图窗口或新的采集授权。

## 当前聊天核图与辅导

课程工作台默认 loopback 18771。先读取其提供的待处理请求、核图 packet 和证据图，或者通过 `scripts/recognition_chat.py`、`scripts/course_tutor.py` 的现有 CLI 操作。核对 raw/坐标/图片 hash、同源版本、完整接触与 preview/final 标记。

转写保留不确定、多区域文字/公式/图形/标注；不能把半式拼成完整公式。反馈引用有效 evidence IDs，保持渐进提示；未完成最终答案判定时使用未评定状态。请求或绑定已 superseded 时重新取得当前版本，不修改旧记录。

网页补强提交经当前聊天批阅和校验入库。新增题册/页绑定、最终擦改对象、长期可靠性均以实际部署版本的交接为准，不能假定已闭环。

## 开发验证

通常使用 `scripts/verify_workspace.py --group core`；可选模型检查需要独立现有 torch 环境。receiver 修改另跑 `verify_pc_pipeline.py` 和 `verify_session_replay.py`，ArkTS/native 修改需 `build-harmony.ps1`。真机写字仅在采集/生命周期改动确需验收时请求，不重复已完成实验。
