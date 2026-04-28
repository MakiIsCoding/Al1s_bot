# AstrBot Source Deployment Plan

本文档描述 `/home/ubuntu/astrbot` 的无 Docker 部署方案。服务器约束是：不安装 Docker，不使用 Docker Compose，不在本机运行 Shipyard 沙盒；AstrBot 使用源码部署，并通过 systemd 用户服务常驻。

## 1. 当前服务器状态

已确认服务器信息：

- 系统：Ubuntu 24.04 LTS。
- 用户：`ubuntu`。
- Docker：未安装。
- UFW：inactive。
- 现有 NoneBot2：监听 `127.0.0.1:8081`。
- 端口 `6185`、`6199` 目前未被占用，可用于 AstrBot WebUI 和 OneBot v11 反向 WebSocket。

不安装 Docker 是当前明确约束，因此本文档所有命令都按源码部署设计。

## 2. 目录规划

目标目录：

```text
/home/ubuntu/astrbot/
  upstream/
    AstrBot/
  data/
  plugins/
  docs/
  logs/
  run/
  backups/
```

目录职责：

- `upstream/AstrBot/`：AstrBot 官方源码仓库。
- `data/`：AstrBot 运行数据、配置、数据库、插件数据等持久化内容。
- `plugins/`：从 botv1 迁移来的自定义插件源码，后续可通过软链接或 AstrBot 插件机制接入。
- `docs/`：部署、迁移、插件设计、运维文档。
- `logs/`：systemd 或应用日志落盘目录。
- `run/`：运行时临时文件，例如 pid、socket、health check 标记。
- `backups/`：迁移前后的数据备份。

## 3. 安装方式

### 3.1 优先方案：uv

官方源码部署文档推荐使用 `uv`：

```bash
cd /home/ubuntu/astrbot/upstream
git clone https://github.com/AstrBotDevs/AstrBot.git
cd /home/ubuntu/astrbot/upstream/AstrBot
uv sync
uv run main.py
```

适用场景：

- 服务器可以访问 GitHub 和 Python 包源。
- 希望依赖解析、虚拟环境和启动方式由 `uv` 管理。

### 3.2 备用方案：venv + pip

如果 `uv` 安装失败或网络不稳定，使用传统虚拟环境：

```bash
cd /home/ubuntu/astrbot/upstream
git clone https://github.com/AstrBotDevs/AstrBot.git
cd /home/ubuntu/astrbot/upstream/AstrBot
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
python main.py
```

适用场景：

- 服务器已有 Python 3.10+。
- 需要更直接地控制依赖安装源。

## 4. 持久化策略

源码部署通常会把运行数据放在项目工作目录下。为避免后续升级源码时误伤数据，建议采用以下策略：

1. `upstream/AstrBot/` 只放官方源码。
2. `/home/ubuntu/astrbot/data/` 保存可变数据。
3. 首次启动前或首次启动后，将 AstrBot 的数据目录迁移/软链接到 `/home/ubuntu/astrbot/data/`。

如果 AstrBot 实际数据目录名为 `data`，则使用：

```bash
cd /home/ubuntu/astrbot/upstream/AstrBot
mkdir -p /home/ubuntu/astrbot/data
if [ -d data ] && [ ! -L data ]; then
  mv data /home/ubuntu/astrbot/backups/data.initial.$(date +%Y%m%d%H%M%S)
fi
ln -sfn /home/ubuntu/astrbot/data data
```

执行前必须确认 AstrBot 当前版本的数据目录位置。该步骤后续在真正安装时再执行。

## 5. systemd 用户服务

不建议继续用 `screen` 作为正式部署方式。推荐使用 systemd 用户服务，避免 root 运行。

服务文件路径：

```text
/home/ubuntu/.config/systemd/user/astrbot.service
```

uv 版本示例：

```ini
[Unit]
Description=AstrBot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/ubuntu/astrbot/upstream/AstrBot
ExecStart=/usr/bin/env uv run main.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
```

venv 版本示例：

```ini
[Unit]
Description=AstrBot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/ubuntu/astrbot/upstream/AstrBot
ExecStart=/home/ubuntu/astrbot/upstream/AstrBot/.venv/bin/python main.py
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

当前 `botv1` 使用 `127.0.0.1:8081`，因此 AstrBot 不要复用 `8081`，避免并行迁移时冲突。

安全策略：

- WebUI 绑定 `127.0.0.1` 最安全，通过 SSH 隧道访问。
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

当前迁移目标是保持 QQ 接入方式不大改，让 NapCat 或其他 OneBot v11 实现端连接到 AstrBot。

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
- 通过后再把正式 NapCat 连接从 `botv1` 切到 AstrBot。

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

1. 创建 `/home/ubuntu/astrbot` 目录和文档。
2. 安装 AstrBot 源码和依赖。
3. 启动 AstrBot WebUI，修改默认账号密码。
4. 配置模型提供商和 Agent Runner。
5. 配置 OneBot v11 测试接入。
6. 验证基础收发消息。
7. 迁移 `bot-llmchat` 的最小文本交互能力。
8. 迁移表情包数据，只读接入。
9. 开发表情/情绪工具，让 Agent 自主决定是否发图。
10. 接入 Web Search，验证复杂问题搜索和总结。
11. 灰度切换正式 QQ 连接。
12. 稳定后停止 `/home/ubuntu/botv1` 的 screen 进程。

## 10. 回滚方案

迁移期间保留 `/home/ubuntu/botv1` 不动。

回滚方式：

1. 停止 AstrBot：

```bash
systemctl --user stop astrbot.service
```

2. 将 NapCat 的反向 WebSocket 地址改回旧 NoneBot 地址。
3. 确认 `screen -ls` 中 `bot` 会话仍在。
4. 如果旧机器人不在，进入 `/home/ubuntu/botv1` 后重新执行：

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

备份：

```bash
tar -czf /home/ubuntu/astrbot/backups/astrbot-data-$(date +%Y%m%d%H%M%S).tar.gz -C /home/ubuntu/astrbot data plugins docs
```

## 12. 待确认项

正式安装前需要确认：

- AstrBot 当前版本源码部署时的确切数据目录。
- AstrBot WebUI 是否支持直接配置监听地址和端口。
- OneBot v11 反向 WebSocket 的实际 path。
- NapCat 当前部署方式、配置文件位置和是否能并行接测试实例。
- 模型提供商选择、API Key 存放位置、是否支持 function calling 和视觉输入。
- 是否需要开放公网访问 WebUI；默认建议不开。

## 13. 参考资料

- AstrBot 源码部署：https://docs.astrbot.app/en/deploy/astrbot/cli.html
- AstrBot OneBot v11 接入：https://docs.astrbot.app/platform/aiocqhttp.html
- AstrBot Agent Runner：https://docs.astrbot.app/use/agent-runner.html
- AstrBot Web Search：https://docs.astrbot.app/use/websearch.html
- AstrBot Computer Use：https://docs.astrbot.app/use/computer.html
- AstrBot Skills：https://docs.astrbot.app/en/use/skills.html
