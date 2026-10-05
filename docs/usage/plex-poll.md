---
title: 🔄 Plex 主动同步
order: 13
---

# 🔄 Plex 主动同步

主动读取 Plex 的已看剧集和电影并同步到 Bangumi，支持手动「标记已看」、导入历史已看项目，以及补上 Webhook 漏掉的记录。无需配置 Webhook，也不依赖 Plex Pass 的 Webhook 功能。可与 [Plex Webhooks](./plex-webhooks) 同时使用。

## 配置方法

在「配置管理 → Plex 主动同步」填写以下字段并保存：

| 配置键（`[plex-poll]`） | 说明 | 默认值 |
| --- | --- | --- |
| `enabled` | 启用定时同步；首次运行会导入所选库的历史已看项目 | `false` |
| `url` | Bangumi-syncer 可以访问的 Plex 服务器地址，如 `http://192.168.1.10:32400`；不能填 `app.plex.tv`，不要带 Token 或其他查询参数 | 空 |
| `token` | 要同步的 Plex 用户的 `X-Plex-Token`，保存时加密存储 | 空 |
| `user_name` | 用于选择 Bangumi 账号的媒体服务器用户名，须与账号设置中的绑定一致 | 空 |
| `library_ids` | 媒体库数字 ID，多个用英文逗号分隔，如 `1,3`。建议只选动画库；留空会读取所有可访问的剧集和电影库 | 空 |
| `sync_interval` | 五段式 Cron，保存后生效 | `*/15 * * * *`（每 15 分钟） |

获取 Token 可参考 [Plex 官方说明](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)。媒体库 ID 可从 Plex Web 打开对应媒体库后的地址中找到 `source` 后面的数字，或从 Plex 的 `/library/sections` 返回结果中查看 `key`。

**观看状态属于 Token 对应的 Plex 用户。** `user_name` 只用于本项目的账号绑定，不会切换 Plex 用户，也不能用管理员 Token 读取其他用户的已看状态。本配置一次连接一个服务器、一个 Plex 用户；家庭成员需要使用其自己的用户 Token。

Docker 中的 `localhost` 指向 Bangumi-syncer 容器本身。Plex 在另一台设备或另一个容器时，请使用可达的局域网地址或容器服务名。

## 运行与历史导入

- **立即同步**：先保存配置，再点击按钮。首次扫描导入全部已看项目，后续跳过已成功同步的项目。也可以等待下一个定时时间。
- **重新核对全部已看**：绕过本地成功记录，重新通过现有匹配流程核对 Bangumi。变更 Bangumi 账号绑定、修正匹配规则或想重新检查历史时使用。
- 两个入口都在后台运行；离开或刷新页面不影响任务，再回到配置页可查看最近一轮结果。在「同步记录」中按「Plex 主动同步」筛选可查看详情。
- 首次导入量大时会分多轮完成，沿用 `[scheduler] job_timeout` 的时间预算，每完成一个条目即保存进度。达到预算后不再开始下一条；已经开始的条目会处理完再停止。后续普通同步会跳过成功条目，继续剩余部分。全量重新核对同样受时间预算限制。

每轮分页读取 Plex 的已看项目，只有 `viewCount > 0` 的剧集和电影参与同步。采用本地成功记录去重，不把 `lastViewedAt` 当作唯一依据，因此手动标记缺少观看时间时也能同步。失败、待补发或部分 Bangumi 账号失败的条目会在后续扫描中重试，成功记录在重启后仍保留。

兼容 Plex 的 JSON 与 XML 返回格式。剧集优先在服务器端筛选已看；旧版本不支持该筛选或筛选结果为空时，会完整读取一次剧集列表并逐条核对已看状态，因此旧版本或尚无已看项目的大型媒体库扫描可能更慢。

沿用项目现有的账号绑定、屏蔽规则、番剧匹配、通知及补发流程。Webhook 与主动扫描同时发现同一集时，由共享同步流程检查 Bangumi 的已看状态。

只做 **Plex → Bangumi 的已看同步**：取消已看不会清除 Bangumi 历史；播放了一部分的项目不会提前标成已看，也不会把电影设为「在看」。重复观看已同步项目不会新增一份观看历史。更换 Plex Token 或服务器后会重新核对已看项目。

## API

需要登录 Web 管理界面或使用项目现有 API 鉴权：

- `POST /api/plex-poll/sync/manual`：启动普通扫描，返回 `202`。
- `POST /api/plex-poll/sync/manual?full=true`：重新核对全部已看。
- `GET /api/plex-poll/status`：查询 `running` 与 `last_result`，不返回 Token。

已有扫描运行时返回 `409`，配置未启用或必填项缺失时返回 `400`。
