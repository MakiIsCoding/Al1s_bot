# NoneBot2 插件迁移总览

## 迁移范围

旧目录：`/home/ubuntu/botv1/src/plugins`

新目录：`/home/ubuntu/astrbot/AstrBot/data/plugins`

| 旧插件 | 新 AstrBot 插件 | 状态 |
| --- | --- | --- |
| `bot-setu` | `al1s_setu` | 已迁移，保留 `/setu` 触发、tag、R18 群白名单和管理员开关 |
| `bot-jmcomic` | `al1s_jmcomic` | 已迁移，保留搜索、查询、下载、群开关、黑名单和禁用标签能力 |
| `bot-dayanime` | `al1s_legacy_tools` | 已迁移，保留 `dayanime`、`今日动画`、`今日新番` |
| `bot-bigimage` | `al1s_legacy_tools` | 已迁移，保留 `图片超分` 后等待图片的交互 |
| `bot-translate` | `al1s_legacy_tools` | 已迁移，保留 `翻译图片` 后等待图片的交互 |
| `bot-summary` | `al1s_legacy_tools` | 已迁移，保留 `总结`，基于 AstrBot 运行期收集的群聊上下文 |
| `bot-help` | `al1s_legacy_tools` | 已迁移，保留 `help`、`帮助`、`菜单`、`功能` |
| `bot-data-cleaner` | `al1s_legacy_tools` | 已迁移，改为插件内定时清理缓存 |
| `bot-emoji-collector` | `al1s_emoji_tools` | 已迁移，保留私聊超级用户表情收集、状态、清理、列表、打标 |
| `superuser-bqb` | `al1s_emoji_tools` | 已迁移，保留私聊 `/addimg`、`/endimg` 图片情绪分类入库 |
| `bot-llmchat` | AstrBot 核心 AI + 人格 + `al1s_emoji_tools` | 框架能力已由 AstrBot 接管；旧表情管理与情绪分类能力迁入 `al1s_emoji_tools` |

## 新插件职责

- `al1s_setu`：Pixiv setu 查询、tag 查询、R18 权限控制。
- `al1s_jmcomic`：JM 搜索、查询、下载、PDF 生成与上传。
- `al1s_legacy_tools`：新番日历、图片超分、图片翻译、聊天总结、帮助菜单、缓存清理。
- `al1s_emoji_tools`：超级用户私聊表情收集、表情库状态管理、视觉模型打标、图片情绪分类入库。

## 运行时配置

配置文件位于 `AstrBot/data/config/*_config.json`，包含 API Key、群白名单、管理员列表等运行时配置，不进入 Git。

运行时数据位于 `AstrBot/data/plugin_data/`，包含缓存、下载文件、表情库、图片分类库，不进入 Git。

仓库只提交 `AstrBot/data/plugins/al1s_*` 下的自研迁移插件源码。

## 验证方式

服务器端语法检查：

```bash
cd /home/ubuntu/astrbot/AstrBot
. .venv/bin/activate
python -m py_compile data/plugins/al1s_emoji_tools/main.py data/plugins/al1s_legacy_tools/main.py data/plugins/al1s_jmcomic/main.py data/plugins/al1s_setu/main.py
```

服务管理：

```bash
screen -r astrbot
```

WebUI：

```text
http://101.43.109.47:6185
```
