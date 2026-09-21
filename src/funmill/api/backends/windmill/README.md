# Windmill 简单部署

以下方式不使用 Docker，适用于 Linux x86_64。需要一套可用的 PostgreSQL，
以及 `python3`、`uv` 和 Bash；Windmill 会自动初始化数据库表。

## 1. 安装 Funmill 和 Windmill

```bash
uv sync
uv run funmill install windmill
```

Windmill 的二进制和配置文件都会放在
`~/.farfarfun/funmill/services/windmill/`。安装器会校验官方发行包的
SHA-256；设置 `FUNMILL_HOME` 可以修改 Funmill 的数据根目录。其他第三方服务
同样使用 `~/.farfarfun/funmill/services/<service>/` 目录。

## 2. 准备数据库

在 PostgreSQL 中创建用户和数据库：

```bash
sudo -u postgres createuser --pwprompt windmill
sudo -u postgres createdb --owner=windmill windmill
```

编辑 `~/.farfarfun/funmill/services/windmill/.env`，写入连接地址：

```dotenv
DATABASE_URL=postgresql://windmill:数据库密码@127.0.0.1:5432/windmill
MODE=standalone
SERVER_BIND_ADDR=0.0.0.0
```

安装器首次创建该文件时会设置 `0600` 权限，且不会覆盖已有配置。
Funmill 启动 Windmill 时会固定使用 `SERVER_BIND_ADDR=0.0.0.0`。

## 3. 启动 Windmill

`standalone` 模式会在一个进程中同时运行 Server 和一个 Worker：

```bash
uv run funmill start windmill
```

该命令会在后台启动 Windmill，并打印 PID 和日志路径。PID 与日志分别保存在
`~/.farfarfun/funmill/services/windmill/windmill.pid` 和 `windmill.log`。
Windmill 的 Web 界面和原生 API 固定监听 `0.0.0.0:8813`。本机打开
<http://127.0.0.1:8813>，首次登录使用：

```text
admin@windmill.dev / changeme
```

立即修改密码，并在 `admins` workspace 中创建 API Token。

## 4. 启动 Funmill API

回到 Funmill 仓库根目录执行：

```bash
FUNMILL_API_KEY='自行设置的接口密钥' \
FUNMILL_BACKEND=windmill \
WINDMILL_URL='http://127.0.0.1:8813' \
WINDMILL_WORKSPACE=admins \
WINDMILL_TOKEN='刚创建的Windmill-Token' \
uv run funmill start
```

Funmill API 固定监听 `0.0.0.0:8812`，并保持前台运行。

验证：

```bash
curl http://127.0.0.1:8812/health
FUNMILL_API_KEY='自行设置的接口密钥' ./scripts/smoke.sh
```

## 5. 管理后台服务

```bash
uv run funmill status windmill
uv run funmill restart windmill
uv run funmill stop windmill
tail -f ~/.farfarfun/funmill/services/windmill/windmill.log
```

重复启动会被 PID 文件拦截。`stop` 会向 Windmill 的独立进程组发送 `SIGTERM`，
等待正常退出后删除 PID 文件；服务异常退出后，`status` 会清理失效 PID。

## 增加 Worker

需要更高并发时，在同一台或其他机器额外启动 Worker。每个进程使用不同的
`WORKER_SUFFIX`：

```bash
MODE=worker WORKER_SUFFIX=worker2 uv run funmill start windmill
MODE=worker WORKER_SUFFIX=worker3 uv run funmill start windmill
```

每个 Worker 使用 `windmill-<WORKER_SUFFIX>.pid` 和同名日志。查询或停止某个
Worker 时需要传入相同环境变量，例如：

```bash
MODE=worker WORKER_SUFFIX=worker2 uv run funmill status windmill
MODE=worker WORKER_SUFFIX=worker2 uv run funmill stop windmill
```

需要开机启动、自动重启和日志轮转时，使用现有的 systemd 或进程管理器直接
管理已安装的 Windmill 二进制。不要在
一个普通 Worker 进程中设置 `NUM_WORKERS>1`；Windmill 会因为隔离安全限制将
它回退为 1，多个独立 Worker 进程更明确。
