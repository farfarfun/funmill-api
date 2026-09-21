# Dagu 简单部署

以下方式不使用 Docker，适用于 macOS 和 Linux 的 x86_64、ARM64 平台。
Funmill 当前固定安装 Dagu `v2.16.3`，运行任务还需要 `python3` 和 Bash。

## 1. 安装 Funmill 和 Dagu

```bash
uv sync
uv run funmill install dagu
```

Dagu 二进制会放在 `~/.farfarfun/funmill/services/dagu/dagu`，运行数据会放在
同目录的 `data/`。安装器会分别校验官方发行包和二进制的 SHA-256；设置
`FUNMILL_HOME` 可以修改 Funmill 的数据根目录。

如果已有文件未通过校验，可明确覆盖安装：

```bash
uv run funmill install dagu --force
```

## 2. 启动 Dagu

```bash
uv run funmill start dagu
```

该命令会在后台启动 Dagu，并打印 PID 和日志路径。PID 与日志分别保存在
`~/.farfarfun/funmill/services/dagu/dagu.pid` 和 `dagu.log`。

Dagu 的 Web 界面和原生 API 固定监听 `0.0.0.0:8813`；本机仍使用
<http://127.0.0.1:8813> 访问。`0.0.0.0` 是监听地址，不能作为客户端地址。
本地启动默认设置 `DAGU_AUTH_MODE=none`，不需要外部数据库；未使用的 Dagu
coordinator 默认关闭。

## 3. 启动 Funmill API

在另一个终端执行：

```bash
FUNMILL_API_KEY='自行设置的接口密钥' \
FUNMILL_BACKEND=dagu \
DAGU_URL='http://127.0.0.1:8813' \
uv run funmill start
```

Funmill API 固定监听 `0.0.0.0:8812`，并保持前台运行。
`DAGU_TIMEOUT` 可以修改 Funmill 请求 Dagu 的超时秒数，默认值为 `30`。
若自行启用了 Dagu 认证，两个启动命令都需要设置相同的 `DAGU_TOKEN`；
Funmill 会将其作为 Bearer Token 使用，并传给等待跨任务依赖的 Dagu 任务。

## 4. 验证

```bash
curl http://127.0.0.1:8812/health
FUNMILL_API_KEY='自行设置的接口密钥' ./scripts/smoke.sh
```

smoke 脚本会验证 Python、Bash 和并行 DAG 的提交、状态、进度、日志与结果。
设置 `FUNMILL_CALLBACK_URL` 还会验证回调；该地址必须能从 Dagu 任务进程访问。

## 5. 管理后台服务

```bash
uv run funmill status dagu
uv run funmill restart dagu
uv run funmill stop dagu
tail -f ~/.farfarfun/funmill/services/dagu/dagu.log
```

重复启动会被 PID 文件拦截。`stop` 会向 Dagu 的独立进程组发送 `SIGTERM`，
等待正常退出后删除 PID 文件；服务异常退出后，`status` 会清理失效 PID。

## 安全与长期运行

Dagu 默认无认证且监听所有网络接口，必须使用防火墙限制 `8813`，或启用 Dagu
认证并配置 `DAGU_TOKEN`。Dagu 会以启动服务的宿主机用户权限直接执行
Funmill 提交的源码，只应接收可信任务。接受不可信代码前必须使用隔离 Worker
或容器，不要仅依赖 Funmill 的 API Key。

Funmill 自带的后台模式适合简单部署。需要开机启动、自动重启和日志轮转时，
使用现有的 launchd、systemd 或进程管理器直接管理已安装的 Dagu 二进制。
