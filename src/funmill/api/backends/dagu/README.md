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

## 0. 配置文件（推荐）

所有 funmill 的配置项都按 **配置文件 > 环境变量 > 默认值** 的优先级读取。配置文件
默认位于 `~/.farfarfun/funmill/api/api.env`（dotenv 格式，支持 `#` 注释、
`export ` 前缀和引号），启动时可用 `--config <路径>` 或 `FUNMILL_CONFIG` 指定别的
路径。`funmill start`、`funmill start dagu` 等所有子命令都会自动读取它，读到时会
打印 `loaded config: <路径>`：

```dotenv
FUNMILL_API_KEY=自行设置的接口密钥
FUNMILL_BACKEND=dagu
DAGU_URL=http://127.0.0.1:8813
DAGU_AUTH_BASIC_USERNAME=funmill
DAGU_AUTH_BASIC_PASSWORD=funmill
# 下发给每个任务子进程的全局环境变量，前缀会被去掉
FUNMILL_TASK_ENV_DJANGO_SETTINGS_MODULE=myapp.settings
```

有了这个文件，后面各节命令前面的那串环境变量都可以省掉，直接
`uv run funmill start dagu` 和 `uv run funmill start` 即可。注意 `FUNMILL_HOME`
本身只能用环境变量设置——它决定了配置文件的位置，不能由配置文件自己定义。

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
本地启动默认开启 Dagu 内置的 HTTP Basic Auth（`DAGU_AUTH_MODE=basic`），
账号默认是 `funmill`/`funmill`（`DAGU_AUTH_BASIC_USERNAME`/
`DAGU_AUTH_BASIC_PASSWORD`），网页和 API 共用同一账号；不需要外部数据库；
未使用的 Dagu coordinator 默认关闭。生产或共享环境务必在启动前自行设置这
两个变量覆盖默认弱密码；只想临时关闭认证（例如纯本地开发）可显式设置
`DAGU_AUTH_MODE=none`。

## 3. 启动 Funmill API

在另一个终端执行：

```bash
FUNMILL_API_KEY='自行设置的接口密钥' \
FUNMILL_BACKEND=dagu \
DAGU_URL='http://127.0.0.1:8813' \
uv run funmill start
```

该命令会在后台启动 Funmill API，并打印 PID 和日志路径（与 `funmill start dagu`
同一套后台生命周期）；改用 `uv run funmill run` 则会在前台运行，方便本地调试
（Ctrl+C 停止）。Funmill API 固定监听 `0.0.0.0:8812`。
`DAGU_TIMEOUT` 可以修改 Funmill 请求 Dagu 的超时秒数，默认值为 `30`。
若自定义了 `DAGU_AUTH_BASIC_USERNAME`/`DAGU_AUTH_BASIC_PASSWORD`，两个启动
命令都需要设置成相同的值：`funmill start` 用它们以 Basic Auth 访问 Dagu
REST API（`DaguBackend.from_env()`），`funmill start dagu` 则会把它们传给
Dagu 进程，Dagu 执行任务时会把自己的环境变量原样传给等待跨任务依赖的
Dagu 任务脚本，脚本读到同一对账号密码后同样以 Basic Auth 访问 Dagu。

## 3.5 任务执行环境

Dagu **不会**把自己的环境变量整体传给任务子进程，而是只放行一个白名单：
`DAGU_*`、Dagu 自己注入的 `DAG_*` 运行元信息，以及 `HOME`、`LANG`、`PATH`、
`PWD`、`SHELL`、`TERM`、`USER`。所以 `DJANGO_SETTINGS_MODULE` 这类变量即使写进
`api.env`、也在 Dagu 进程里存在，任务里依然读不到——必须显式声明，两种方式：

- 提交任务时传 `env`（`TaskSubmit.env` / 工作流里每个 task 的 `env`），按请求生效。
- 在 `api.env` 里写 `FUNMILL_TASK_ENV_<变量名>=值`，对所有任务生效。前缀会被去掉，
  即 `FUNMILL_TASK_ENV_DJANGO_SETTINGS_MODULE=myapp.settings` 在任务里就是
  `DJANGO_SETTINGS_MODULE`。这批默认值在 Funmill API 启动时读取一次，改完要重启
  `funmill start`。

同名时**按请求传的 `env` 覆盖全局默认值**。变量名必须是合法 shell 标识符
（`^[A-Za-z_][A-Za-z0-9_]*$`）；没有 `FUNMILL_TASK_ENV_` 前缀的普通变量一律不会
下发给任务。

任务用的 Python 解释器是继承到的 `PATH` 里第一个 `python3`，而这个 `PATH` 就是
`funmill start dagu` 当时的 `PATH`——所以要在装好依赖的那个环境里启动 Dagu。任务的
工作目录是 Dagu 为每次运行新建的临时目录（`data/dag-run-work/...`），每次运行独立。

## 4. 验证

```bash
curl http://127.0.0.1:8812/health
curl http://127.0.0.1:8813/api/v1/health         # 未带账号密码，预期 401
curl -u funmill:funmill http://127.0.0.1:8813/api/v1/health  # 预期 200
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

Dagu 默认开启 Basic Auth，但出厂账号密码都是 `funmill`，且监听所有网络接口，
生产或共享环境必须同时做到两件事：改掉默认账号密码
（`DAGU_AUTH_BASIC_USERNAME`/`DAGU_AUTH_BASIC_PASSWORD`），并用防火墙限制
`8813` 只对可信来源开放。Dagu 会以启动服务的宿主机用户权限直接执行
Funmill 提交的源码，只应接收可信任务。接受不可信代码前必须使用隔离 Worker
或容器，不要仅依赖 Funmill 的 API Key。

Funmill 自带的后台模式适合简单部署。需要开机启动、自动重启和日志轮转时，
使用现有的 launchd、systemd 或进程管理器直接管理已安装的 Dagu 二进制。
