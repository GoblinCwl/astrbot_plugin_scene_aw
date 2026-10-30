# AstrBot ActivityWatch Scene Awareness

将 ActivityWatch 采集的桌面活动转化为 AstrBot Agent 可使用的实时场景：当前事务、窗口焦点、应用分类、离席状态和最近活动概况。

## 功能

- 增量读取 ActivityWatch 的窗口、AFK 及增强上下文事件
- 使用可选 LLM 将最近活动归纳为一个主事务和最多两个并行事务
- 在每轮 LLM 请求前注入当前场景，不写入对话历史
- 维护 ActivityWatch 应用分类，支持置信度阈值和手动确认
- 可通过 Tavily 搜索辅助识别陌生应用，仅提交应用文件名
- 发现新应用后可向指定 AstrBot 会话合并发送分类提醒
- 提供当前场景查询、分类确认和放弃确认三个 Agent Tools
- 提供插件管理面板，查看状态并处理待确认分类
- ActivityWatch 离线或用户 AFK 时停止输出过期场景

## 依赖与兼容性

- AstrBot `>=4.27.3,<5`
- Python 3.10 或更高版本（随 AstrBot 运行环境）
- 一个 AstrBot 运行环境可访问的 [ActivityWatch](https://activitywatch.net/) Server
- Python 包 `aiohttp>=3.9,<4`（安装插件时由 AstrBot 自动处理）
- 场景识别和应用 AI 分类需要已在 AstrBot 配置的 LLM Provider
- 联网辅助分类为可选功能，需要 Tavily API Key

插件通过 ActivityWatch HTTP API 工作，不要求 ActivityWatch 与 AstrBot 采用相同部署方式。默认地址是 `http://127.0.0.1:5600`；如果 AstrBot 运行在 Docker 中，`127.0.0.1` 指向容器本身，请改为容器能够访问的宿主机地址，并确认 ActivityWatch Server 已允许该网络连接。

## 安装

在 AstrBot WebUI 插件市场安装，或手动克隆到插件目录：

```bash
cd AstrBot/data/plugins
git clone https://github.com/GoblinCwl/astrbot_plugin_scene_aw.git
```

随后在 AstrBot WebUI 中重载插件。若依赖没有自动安装，可在 AstrBot 环境中执行：

```bash
pip install -r data/plugins/astrbot_plugin_scene_aw/requirements.txt
```

## 快速配置

1. 启动 ActivityWatch，并确认 AstrBot 所在环境可以访问其 HTTP API。
2. 设置 `aw_base_url`；非容器部署通常可保留默认值。
3. 为 `scene_llm_provider_id` 选择一个 AstrBot LLM Provider。
4. 如需自动维护应用分类，再设置 `category_llm_provider_id`。
5. 重载插件，在插件管理页面打开本插件的管理面板检查连接与分类状态。
6. 确认运行稳定后，再按需开启新应用主动提醒。

## 主要配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `aw_base_url` | `http://127.0.0.1:5600` | AstrBot 可访问的 ActivityWatch Server 地址 |
| `category_sync_aw_base_url` | 空 | 可选的分类同步 ActivityWatch Server；留空时不同步，每次分类写入时会 POST 完整 `category_sets` 和 `active_set_ids` |
| `scan_interval_seconds` | `30` | ActivityWatch 增量扫描间隔，最小按 10 秒执行 |
| `startup_lookback_minutes` | `10` | 启动首次扫描的回溯时间 |
| `aw_offline_after_seconds` | `180` | 无 heartbeat 后判定离线的时间 |
| `scene_llm_provider_id` | 空 | 场景分析使用的 AstrBot Provider |
| `category_llm_provider_id` | 空 | 应用分类使用的 AstrBot Provider |
| `context_injection_enabled` | `true` | 向本轮 LLM 请求注入实时场景 |
| `category_classify_on_startup` | `false` | 启动时自动分类全部未知应用 |
| `category_auto_apply_confidence` | `0.9` | 普通 AI 分类自动写回的置信度阈值 |
| `tavily_api_key` | 空 | 启用陌生应用联网辅助识别 |
| `category_web_auto_apply_confidence` | `0.5` | 联网识别结果自动写回阈值 |
| `new_app_notification_enabled` | `false` | 主动发送新应用分类提醒 |
| `new_app_notification_unified_msg_origin` | 空 | 主动提醒的目标会话 UMO |

其余灵敏度、统计窗口、分类定义和提示词均可在 AstrBot 插件配置页调整。建议先使用默认值运行一段时间，再根据 ActivityWatch 数据密度微调。

## 新应用提醒

主动提醒默认关闭。启用前需要填写 `new_app_notification_unified_msg_origin`。插件发现尚未分类的新应用后，会等待一段安静时间，将多个应用合并为一条消息。

如果同时配置 Tavily、Category Provider 和分类提示词，插件会先尝试生成建议分类；否则仍会发送不含建议的提醒。Agent 只能在配置的目标会话中确认分类，且应用必须处于本次主动确认范围内。

## Agent Tools

| Tool | 作用 |
| --- | --- |
| `scene_aw_get_current_scene` | 查询实时 AW 状态、场景、焦点和更新时间 |
| `scene_aw_confirm_app_category` | 在用户明确确认后写入应用分类 |
| `scene_aw_dismiss_app_category` | 结束本次对话确认，保留网页待确认项 |

## 隐私与安全

- 窗口标题、应用名称和可选 OCR 关键词可能包含敏感信息；请保护 AstrBot、ActivityWatch 和模型服务的访问权限。
- 启用场景分析后，最近活动摘要会发送给所选 LLM Provider。
- Tavily 辅助分类只搜索应用文件名，不提交窗口标题。
- 不建议将 ActivityWatch HTTP API 直接暴露到公网；跨主机访问时应使用可信网络或反向代理访问控制。
- 插件运行态通知记录位于插件 `data` 目录，并已通过 `.gitignore` 排除，发布前不要提交本机生成的数据。

## 发布前检查

```bash
python3 -m compileall .
python3 -m json.tool _conf_schema.json >/dev/null
```

还应在实际 AstrBot 环境中验证：插件加载、ActivityWatch 连接、管理面板、一次场景识别、一次手动分类，以及启用后的一次主动提醒。

## 开发说明

本项目采用纯 **Vibe Coding** 方式开发，代码与文档由 AI 根据维护者需求生成和编辑，并由维护者在实际 AstrBot 与 ActivityWatch 环境中测试、验收和决定发布。使用或贡献代码时，请独立检查其安全性、兼容性与行为。
