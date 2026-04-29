# AstrBot Source Deployment Plan

本文档描述 `/home/ubuntu/astrbot` 的无 Docker 部署方案。服务器约束是：不安装 Docker，不使用 Docker Compose，不在本机运行 Shipyard 沙盒；AstrBot 官方源码放在外层项目的 `AstrBot/` 子目录内，并通过 systemd 用户服务常驻。

## 1. 当前服务器状态

已确认服务器信息：

- 系统：Ubuntu 24.04 LTS。
- 用户：`ubuntu`。
- Docker：未安装。
- UFW：inactive。
- 现有 NoneBot2：已停止，原端口为 `127.0.0.1:8081`。
- 端口 `6185`、`6199` 当前可规划给 AstrBot WebUI 和 OneBot v11 反向 WebSocket。
- GitHub 直连：当前不可用，clone 官方仓库会超时。
- PyPI/清华源：可用，已能下载 `astrbot==4.23.6`。

不安装 Docker 是当前明确约束，因此本文档所有命令都按源码部署设计。

## 2. 目录规划

目标目录：

```text
/home/ubuntu/astrbot/
  .codex/
    skills/
      gm/
      pr/
  docs/
    overview.md
    deployment.md
    migration-map.md
    plugin-design.md
    operations.md
  AstrBot/
    main.py
    pyproject.toml
    data/
      plugins/
        astrbot_plugin_llmchat_agent/
        astrbot_plugin_emoji/
      config/
      plugin_data/
```

目录职责：

- `.codex/`：Codex 自动化 skill，例如 `gm` 和 `pr`。
- `docs/`：迁移、部署、插件设计、运维文档。
- `AstrBot/`：官方 AstrBot 源码目录，当前由 PyPI 官方源码包 `astrbot==4.23.6` 解压得到。
- `AstrBot/data/`：AstrBot 自己的持久化数据目录。
- `AstrBot/data/plugins/`：AstrBot 插件目录，我们迁移后的自定义插件放这里。
- `AstrBot/data/plugin_data/`：插件运行数据，避免把运行数据写入插件源码目录。

这个结构不使用根级 `data/`、`plugins/`、`backups/`，也不使用 `upstream/`。

## 3. 安装方式

### 3.1 获取 AstrBot 源码

当前服务器无法直连 GitHub，因此使用官方 PyPI 源码包初始化 `AstrBot/`：

```bash
cd /home/ubuntu/astrbot
python3 -m pip download --no-deps --no-binary=:all: astrbot==4.23.6 \
  -i https://pypi.tuna.tsinghua.edu.cn/simple \
  -d .tmp-astrbot-download
mkdir -p AstrBot
tar -xzf .tmp-astrbot-download/astrbot-4.23.6.tar.gz -C AstrBot --strip-components=1
rm -rf .tmp-astrbot-download
```

运行时 `AstrBot/` 就是完整源码目录。后续如果 GitHub 网络恢复，可以再评估是否改成 submodule；当前先以可运行和可维护为优先。

### 3.2 当前安装方式：venv + editable install

服务器当前未安装 `uv`，实际使用 Python venv 和 editable install：

```bash
cd /home/ubuntu/astrbot/AstrBot
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip -i https://pypi.tuna.tsinghua.edu.cn/simple
python -m pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 3.3 初始化

初始化 AstrBot 根目录：

```bash
cd /home/ubuntu/astrbot/AstrBot
. .venv/bin/activate
printf 'y\n' | astrbot init
```

PyPI 源码包里带有离线 Dashboard artifact。因为 GitHub 不通，需要先放置本地 Dashboard dist，避免初始化/启动时尝试下载 Dashboard：

```bash
cd /home/ubuntu/astrbot/AstrBot
rm -rf astrbot/dashboard/dist
cp -a dashboard-artifact/unpacked/dist astrbot/dashboard/dist
```

## 4. 持久化策略

不做软链接。AstrBot 的数据和插件按官方默认目录放在源码目录内部：

```text
/home/ubuntu/astrbot/AstrBot/data/
  plugins/
  config/
  plugin_data/
```

维护原则：

- 官方源码和官方数据目录保持 AstrBot 默认布局。
- 自定义插件放在 `AstrBot/data/plugins/`。
- 插件运行数据放在 `AstrBot/data/plugin_data/`。
- 不把 API Key、Cookie、账号密码写入文档或提交到 Git。
- 后续升级官方 AstrBot 时，优先下载新版 PyPI 源码包覆盖源码文件；执行前先保留 `data/` 和 `.astrbot`。

## 5. systemd 用户服务

不建议继续用 `screen` 作为正式部署方式。推荐使用 systemd 用户服务，避免 root 运行。

服务文件路径：

```text
/home/ubuntu/.config/systemd/user/astrbot.service
```

venv 版本示例：

```ini
[Unit]
Description=AstrBot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/ubuntu/astrbot/AstrBot
ExecStart=/home/ubuntu/astrbot/AstrBot/.venv/bin/python main.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
```

启用命令：

```bash
mkdir -p /home/ubuntu/.config/systemd/user
systemctl --user daemon-reload
systemctl --user enable --now astrbot.service
systemctl --user status astrbot.service
```

如果希望 SSH 退出后用户服务仍然随系统运行，需要 root 执行一次：

```bash
sudo loginctl enable-linger ubuntu
```

## 6. 端口规划

建议端口：

- `6185`：AstrBot WebUI。
- `6199`：OneBot v11 反向 WebSocket。

当前 `botv1` 原使用 `127.0.0.1:8081`，AstrBot 不复用 `8081`，避免回滚时端口冲突。

安全策略：

- WebUI 优先绑定 `127.0.0.1`，通过 SSH 隧道访问。
- 如果 WebUI 必须绑定公网地址，必须改默认密码，并在云安全组限制来源 IP。
- OneBot v11 端口可以只绑定本机，前提是 NapCat 与 AstrBot 在同一台服务器。

本机访问 WebUI 的 SSH 隧道示例：

```powershell
ssh -L 6185:127.0.0.1:6185 my-ubuntu
```

浏览器访问：

```text
http://127.0.0.1:6185
```

## 7. OneBot / NapCat 接入

迁移目标是保持 QQ 接入方式尽量不变，让 NapCat 或其他 OneBot v11 实现端连接到 AstrBot。

推荐方向：

1. AstrBot 创建 OneBot v11 平台实例。
2. AstrBot 监听 `127.0.0.1:6199` 或 `0.0.0.0:6199`。
3. NapCat 配置反向 WebSocket 到 AstrBot。

如果 NapCat 和 AstrBot 同机运行，连接地址优先使用：

```text
ws://127.0.0.1:6199/ws
```

如果 AstrBot 实际路径或端口由 WebUI 生成，应以 AstrBot WebUI 中的平台配置为准。

迁移期间不要直接切换正式 QQ 号：

- 先用测试号或测试群验证。
- 只验证 AstrBot 接收消息、发送消息、图片消息、表情消息、长文本回复。
- 通过后再把正式 NapCat 连接从旧 NoneBot 地址切到 AstrBot。

## 8. Agent 能力配置

第一阶段目标是让 AstrBot 具备比 NoneBot 更强的 AI 交互能力，但避免一开始开放高风险执行。

推荐初始配置：

- 启用 Built-in Agent Runner。
- 配置支持 function calling 的主模型。
- 启用 Web Search，优先 Tavily。
- 启用 Skills，用于沉淀固定操作流程。
- `Computer Use Runtime` 初始设为 `none`。

后续按需开放：

- `local`：只允许超级用户触发，且工具白名单、超时和审计日志必须先完成。
- `sandbox`：当前服务器不使用 Docker，因此暂缓；如果确实需要隔离执行，后续评估独立机器或远端沙盒。

自主图片与情绪决策需要自定义插件配合：

- 情绪分析工具：根据消息上下文输出情绪标签。
- 表情检索工具：从迁移后的表情库中选候选图片。
- 发送决策工具：控制发送频率、群聊冷却、用户权限。
- 图片发送工具：通过 AstrBot 平台能力发送图片消息。

## 9. 迁移执行顺序

推荐顺序：

1. 将官方 AstrBot 添加到 `/home/ubuntu/astrbot/AstrBot`。
2. 安装 AstrBot 源码依赖。
3. 启动 AstrBot WebUI，修改默认账号密码。
4. 配置模型提供商和 Agent Runner。
5. 配置 OneBot v11 测试接入。
6. 验证基础收发消息。
7. 迁移 `bot-llmchat` 的最小文本交互能力。
8. 迁移表情包数据，只读接入。
9. 开发表情/情绪工具，让 Agent 自主决定是否发图。
10. 接入 Web Search，验证复杂问题搜索和总结。
11. 灰度切换正式 QQ 连接。
12. 稳定后归档旧 `/home/ubuntu/botv1`。

## 10. 回滚方案

迁移期间保留 `/home/ubuntu/botv1` 不动。

回滚方式：

1. 停止 AstrBot：

```bash
systemctl --user stop astrbot.service
```

2. 将 NapCat 的反向 WebSocket 地址改回旧 NoneBot 地址。
3. 如果旧机器人不在，进入 `/home/ubuntu/botv1` 后重新执行：

```bash
cd /home/ubuntu/botv1
. .venv/bin/activate
nb run
```

## 11. 运维命令

查看状态：

```bash
systemctl --user status astrbot.service
```

查看日志：

```bash
journalctl --user -u astrbot.service -f
```

重启：

```bash
systemctl --user restart astrbot.service
```

停止：

```bash
systemctl --user stop astrbot.service
```

检查端口：

```bash
ss -lntp | grep -E '6185|6199'
```

## 12. 待确认项

正式安装前需要确认：

- AstrBot 当前版本源码部署时的实际数据目录。
- AstrBot WebUI 是否支持直接配置监听地址和端口。
- OneBot v11 反向 WebSocket 的实际 path。
- NapCat 当前部署方式、配置文件位置和是否能并行接测试实例。
- 模型提供商选择、API Key 存放位置、是否支持 function calling 和视觉输入。
- 是否需要开放公网访问 WebUI；默认建议不开。

## 13. 当前验证结果

已完成的验证：

- `AstrBot/` 已由 `astrbot-4.23.6.tar.gz` 官方源码包初始化。
- `AstrBot/.venv` 已安装依赖，并以 editable 方式安装 `AstrBot==4.23.6`。
- `astrbot --help` 可正常输出 CLI 命令。
- `astrbot init` 已创建 `.astrbot`、`data/config`、`data/plugins`、`data/temp`。
- 已从 `dashboard-artifact/unpacked/dist` 放置离线 Dashboard 到 `astrbot/dashboard/dist`。
- `timeout 35s astrbot run -p 6185` 可启动 WebUI，日志显示 `AstrBot v4.23.6 WebUI 已启动`，监听 `http://0.0.0.0:6185`。

## 14. 参考资料

- AstrBot 源码部署：https://docs.astrbot.app/en/deploy/astrbot/cli.html
- AstrBot 包管理器部署：https://docs.astrbot.app/en/deploy/astrbot/package.html
- AstrBot OneBot v11 接入：https://docs.astrbot.app/platform/aiocqhttp.html
- AstrBot Agent Runner：https://docs.astrbot.app/use/agent-runner.html
- AstrBot Web Search：https://docs.astrbot.app/use/websearch.html
- AstrBot Computer Use：https://docs.astrbot.app/use/computer.html
- AstrBot Skills：https://docs.astrbot.app/en/use/skills.html
