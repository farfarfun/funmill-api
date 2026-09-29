# Dagu 简单部署

以下方式不使用 Docker，适用于 macOS 和 Linux 的 x86_64、ARM64 平台。
Funmill 默认安装 Dagu `2.17.2`（四个平台包都已发布的最新公共版本），
运行任务还需要 `python3` 和 Bash。

安装器通过 pnpm 从 npm registry 拉取对应平台的 `@dagucloud/dagu-*` 包
（二进制直接打包在 npm 包内），不再直接访问 GitHub Releases，国内网络下更
稳定。使用前需要先安装好 Node.js 与 [pnpm](https://pnpm.io/installation)
并确保 `pnpm` 在 `PATH` 中。如果 pnpm 本身访问 npm 官方 registry 也慢或超
时，可配置国内镜像：

```bash
pnpm config set registry https://registry.npmmirror.com
```

## 1. 安装 Funmill 和 Dagu

```bash
uv sync
uv run funmill install dagu
```

Dagu 二进制会放在 `~/.farfarfun/funmill/services/dagu/dagu`，运行数据会放在
同目录的 `data/`，pnpm 的安装产物则保留在同目录的 `npm/`。二进制的完整性由
npm/pnpm 内置的 SHA-512 校验（对照 registry 元数据）保证，Funmill 不再自行
维护校验和表。设置 `FUNMILL_HOME` 可以修改 Funmill 的数据根目录。

`--force` 会重新安装，并改为拉取该平台包在 npm 上的 `latest` 版本（而不是
上面固定的 `2.17.2`），用于升级到最新可用版本：

```bash
uv run funmill install dagu --force
```

因为各平台包各自独立发布，`--force` 装到的具体版本号可能因平台而略有差异
（例如某个补丁版本尚未覆盖到全部四个平台时）。

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
