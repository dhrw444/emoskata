# emoskata

**TG 签到 Bot：留空 TG ID 公开使用，填写 ID 限定本人；每位用户的账号独立管理。**

Python 3.10+，零第三方 Python 依赖。运行核心只需 `emoskata.py` 与你填好的 `.env`；托管平台首次检查依赖文件时，还要把仓库的 `requirements.txt` 放在实际运行目录。该文件只含注释，没有需安装的包。Node 和旁边的维护模块不是运行依赖。调度、Bot 和网页服务在一个应用进程内运行；重启预检会临时创建子进程。

> 公开使用不等于共享账号或公开管理员面板：每个 TG 用户先通过官方授权建立自己的主号，再添加本人持有 Token 的其他账号。历史跨 TG 共享入口保持关闭。服务器公网 IP 独立探测；CDN 域名只决定访问链接，配置域名不取消探测。更新及数据保留见 [验证记录](VALIDATION.md) 和 [维护地图](MAINTENANCE.md)。

## 配置：只有 `.env` 一个运行来源

运行配置写在 `.env`（默认入口文件同目录）。**程序只读，不创建也不自动改写它**：`.env` 由你复制仓库里的 [`.env.example`](.env.example) 填好后与程序一起上传，权限设 `0600`。`EMOS_ENV_FILE` 只选择配置路径，不充当第二套业务配置。

进程环境里的同名变量**不会**被继承成配置——`.env` 是唯一来源。缺 `.env` 时程序直接停下并提示复制模板，不会替你生成。

文件里少写的键按默认值补进**内存**，读取不会改原文件，所以升级新增字段不需要补写配置。管理员网页保存继续走加锁、校验和原子替换；显式保存缺失键的默认值也会写盘，重启后仍算你的选择。一次性旧库设置迁移同样通过这条更新入口补缺失键；预检只写隔离副本。标为“重启生效”的项保存后需重启核心，其余在下一次读取时生效。

| 键 | 默认 | 说明 |
|---|---|---|
| `PANEL_PASSWORD` | 空（必须自己填） | 管理员密码，至少 8 字符；开网页时为空拒绝启动 |
| `HTTP_ENABLED` | `1` | `0` 关闭网页面板，只留 Bot 与调度（此时必须配 `TG_BOT_TOKEN`） |
| `HTTP_HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8080` | 监听端口 |
| `DATA_DIR` | `./data` | SQLite 数据目录 |
| `TG_BOT_TOKEN` | 空 | BotFather 签发的 Token |
| `TG_ALLOWED_USER_IDS` | 空 | **留空公开使用，填写一个正整数仅允许该 TG ID**；非法值和多个 ID 拒绝。公开模式通知发各账号本人 |
| `TG_API_BASE_URL` | `https://api.telegram.org` | Telegram API 地址 |
| `EMOS_USER_ID` | 空 | 指定 TG ID 模式下 `/login` 的默认本人 emos ID；公开模式各用户输入自己的 ID，不能共用管理员默认 ID |
| `EMOS_AUTH_BOT` | `emospg_bot` | emos 官方授权 Bot 用户名，不含 `@`；官方换名只改这里 |
| `EMOS_BASE_URL` | `https://emos.best` | emos API 地址；也是 Bot 里「在官网查看」的链接；自定义站不向公共站降级 |
| `WEB_BASE_URL` | 空 | 个人网页的 HTTPS 域名/CDN 入口，不影响服务器公网 IP 探测 |
| `PUBLIC_BASE` | 空 | 管理员 HTTPS 域名/CDN 入口；公网 IP 仍独立探测和显示 |
| `SIGN_TIME` | `08:00` | 默认签到时间，强制不早于 `01:00` |
| `SIGN_TIMEZONE` | `Asia/Shanghai` | 调度与日期判定时区 |
| `GAP_SECONDS` | `8` | 多账号之间的间隔秒数 |
| `EMOS_TIMEOUT` | `20` | emos 请求超时 |
| `RETRY_DELAYS` | `300,900,2700` | 失败退避秒数 |
| `BOT_WORKERS` | `2` | Bot 操作线程数 |
| `BOT_MAX_JOBS` | `16` | Bot 待办上限 |
| `TG_POLL_TIMEOUT` | `45` | 长轮询等待秒数 |
| `SCHEDULER_INTERVAL` | `60` | 本地调度检查间隔（只查本地库，不轮询官方接口） |
| `HTTP_REQUEST_TIMEOUT` | `15` | 网页请求超时 |
| `HTTP_MAX_CONNECTIONS` | `8` | 网页并发上限 |
| `ADMIN_SESSION_HOURS` | `24` | 管理员会话时长 |
| `WEB_SESSION_HOURS` | `168` | 个人网页会话时长 |
| `SHUTDOWN_TIMEOUT` | `90` | 软重启收尾上限 |

**没有 `EMOS_APP_USER_ID`。** 官方机器人授权链接里的用户 ID 是**每个用户自己的** emos 用户 ID，不是运营者的固定配置。

## 登录：首个账号官方授权，后续 Token 添加

1. 私聊 `/login`。公开模式会提示发送**你自己的** emos ID，也可直接 `/login 你的ID`；指定 TG ID 模式可在 `.env` 填 `EMOS_USER_ID` 后直接获得授权链接。这里填写官网账号卡片的 ID，不是 Token。
2. Bot 调用 `getMe` 获得用户名，生成官方协议链接：

   ```text
   https://t.me/emospg_bot?start=link_[自己的emos用户ID]-[本Bot用户名]
   同意 → 本Bot?start=emosLinkAgree-[emosToken]
   拒绝 → 本Bot?start=emosLinkRefuse-[TelegramID]
   ```

3. 在 `@emospg_bot` 同意后返回。应用核验 `GET /api/user` 的账号 ID、TG 绑定及待授权时限，事务内建立官方主账号证明。
4. 其他账号输入 `/token` 后粘贴该账号的**官方 emos Token**，或使用 `/token Token`。每个 Token 对应一个独立 emos 账号，不要求这些账号绑定同一个 TG；同一官方账号不能被另一个 TG 用户重复占用。公开模式每个人只管理自己登记的账号，指定模式仅配置的 ID 能操作。
5. 所有登记的账号都能查看、切换、分别修改签到设置。`/accounts` 选择账号，`/sign` 只签当前账号；`/signall` 签全部有效账号。Bot 与网页都有「签到当前账号」「签到全部账号」两个按钮。
6. 已有主账号后，`/login` 只重新授权该主账号。切换到其他账号也不会把它误当作主账号；更新其他账号 Token 使用 `/token`。

授权/输入 10 分钟有效。密钥消息尽力删除，删除失败不会重复添加；回复和日志不回显原始密钥。旧数据迁移不自动冒充“已完成官方首登”，需先官方重新授权。

### Telegram 的 `/` 命令菜单

公开模式的 `setMyCommands` 注册全部私聊作用域，菜单按钮为指令列表；指定模式只为配置 ID 的私聊作用域注册。权限仍在每次消息/回调中核对 `from.id` 和私聊 ID，菜单范围不是访问控制。

```text
/start /menu /login /token /sign /signall /accounts
/settings /time /logs /web /logout /cancel /help
```

命令名称按 Telegram 官方 `BotCommand` 规范：无前导 `/`，1–32 位小写字母、数字或下划线；描述 1–256 字。显示层自动带 `/`。

## 签到：普通 / 随机字

官方接口（Postman 集合原文）：

```text
PUT /api/user/sign?content
描述：禁止接口轮训 发现做封禁处理
      如果是搞自动签到 禁止选0点
query content：说点什么进行随机签到以获得更多萝卜
200 → {sign_index:int, earn_point:int, continuous_days:int}
429 → {message:"重复签到"}
```

所以：

- **普通签到**：不发 `content` 参数，请求体为空。
- **随机字签到**：从内置词表随机取一句作为 `content`（query 参数），自动签到按此执行。
- `/sign 任意文字`：只为当前选中账号执行本次带附言签到，不修改保存模式；不会顺带签到其他账号。`/signall` 则全部账号按各自保存模式执行。
- 每个账号分别保存模式（`sign_mode`）。
- 奖励由官方决定；随机字**不保证**多给萝卜，一切按官方返回记账。
- 内容上限 256 字、不允许控制字符（本程序的额外限制，官方文档未规定长度）。

判断今天是否已签：优先看 `GET /api/user` 返回体里的 `sign` 字段（官方原文：「签到信息 当天未签到时返回为 null」）。字段缺失时 fail-closed；`sign` 是空对象 `{}` **不算**已签（官方只说未签到返回 null，空对象不是已签证据，当成已签会白丢一天）。`429` **只有**在 `PUT /api/user/sign` 且 `message` 为「重复签到」时才算已签；其它 429 一律按限流退避。

`GET /api/sign/check` 的 `is_sign` 是**是否登录**（官方分类在「登录相关」），不能用来判断今天签没签。

## 全部账号、余额与个人网页

- `/accounts` 显示全部账号、官方 ID 和缓存余额。未知余额写“余额未知”，不拿 0 冒充；真实 0 正常显示。
- `carrot` 是官方可用余额；`last_carrot` 是最近单次所得。网页和管理表格分别标注，不把单次 +3 写成总余额 3。
- `/web` 发一次性入口链接。兑换、认证、设置和签到逐次核对当前访问策略及账户归属；改成指定 ID 后其他人的旧会话立即无权，改回留空公开不会删除任何人数据。
- 管理员网页始终需要 `PANEL_PASSWORD`。公开模式显示脱敏管理总览，指定模式只显示指定 ID 的账号；TG 公开不等于公开管理员权限。
- `/link`、`/links`、`/code` 以及历史关联回调关闭；原 SQLite 链接、码、申请和账号不硬删除，调度忽略旧跨 TG 授权。
- 每个 TG 用户没有仍具有效本地登录的官方主号时，禁止添加额外 Token 和自动签到。退出保留首号证明，但证明不等于有效登录。公开模式调度每个人自己的有效账号，不采用历史跨 TG 共享关系。

## 部署

### 单文件（服务器）

```sh
cp .env.example .env
# 编辑 .env：填 PANEL_PASSWORD、TG_BOT_TOKEN；TG_ALLOWED_USER_IDS 留空公开或填一个指定 ID
chmod 600 .env
python3 emoskata.py --prepare-host
python3 emoskata.py --check-config
python3 emoskata.py
```

`.env` 必须与入口同目录，或用 `EMOS_ENV_FILE` 指定原配置的绝对路径。`--check-config` 只读校验，不创建数据目录、数据库、成功快照或服务线程。

### 单文件（容器）

```sh
mkdir -p config data runtime
cp emoskata.py requirements.txt runtime/
cp .env.example config/.env        # 填密码、Bot Token；TG ID 留空公开或填一个指定 ID
chmod 600 config/.env
docker compose up -d
```

`config/.env` 必须由你自己准备；程序不会生成它。上传/写好后可以先只校验不改文件：

```sh
EMOS_ENV_FILE="$PWD/config/.env" python3 runtime/emoskata.py --check-config
```

`docker-compose.yml` 已配好三个挂载：

| 目录 | 内容 |
|---|---|
| `config/` | `.env`（唯一配置） |
| `data/` | `data.db`（账号、官方主号证明、会话、日志及保留历史） |
| `runtime/` | `emoskata.py` 与 `requirements.txt` |

`prepare_docker.sh` 只补缺失的 `.env.example`、`runtime/emoskata.py` 和 `runtime/requirements.txt`，不覆盖已有文件；`config/.env` 缺失时直接报错退出，不会替你创建。

### 纯 Bot（不开网页）

在**已有 `.env` 中只修改** `HTTP_ENABLED=0`，保留原密码、Token、TG ID 和 `DATA_DIR`；不要用重定向覆盖整份配置。配置好以后启动：

```sh
EMOS_ENV_FILE=/app/config/.env python3 /app/runtime/emoskata.py
```

`HTTP_ENABLED` 应写进 `.env`；进程环境不参与配置。纯 Bot 必须有 Token，TG ID 留空即可公开；非法 ID 拒绝启动。纯 Bot 模式目前没有等价的 Bot 核心重启命令。

### 分文件

```sh
python3 app.py
```

## 公网 IP、容器 IP 与 CDN

三个地址独立，不能混用：

- **服务器公网 IP**：正常启动就绪后独立探测一次。配置了域名或 CDN 仍执行；探测结果只作源站/出口 IP 显示，不修改 `.env`，不自动改 CDN DNS。
- **管理访问地址**：有 `PUBLIC_BASE` 时使用你的 HTTPS 域名/CDN；没填时用探测到的公网 IP 与监听端口。`WEB_BASE_URL` 单独决定个人登录链接。
- **容器内网 IP**：`172.19.*` 等仅保留为诊断信息，不再当“管理入口”打印。

公网探测使用两个固定 HTTPS 服务、短超时及响应上限，成功/失败都缓存，不按面板刷新反复请求。NAT 出口 IP 不是入站可达性的证明；宿主端口映射、防火墙与 CDN 回源端口仍需真实连通。Compose 默认发布 `8080:8080`，支持公网 IP 直连；只允许反代访问时可自行改为 `127.0.0.1:8080:8080`。

## 托管平台的 requirements.txt

日志“requirements.txt 文件不存在”不是 `ModuleNotFoundError`：平台在执行 py **之前**检查依赖清单。零包项目仍需这个文件。

- 首次上传：把 `requirements.txt` 与 `emoskata.py` 放进平台实际运行目录。
- 或先在终端执行 `python3 emoskata.py --prepare-host`，仅补齐入口目录和工作目录的清单；不需要 `.env`、不建库、不发网络请求。
- 正常启动也会补缺失清单，但程序不能消除已经在它启动前打印的第一次提示。
- 安装脚本、容器镜像和 `runtime/` 挂载目录都携带清单，已有内容不覆盖。不自动下载任何不存在的 Python 包，也不生成 `.env`。

## 更新：只替换 py

`emoskata.py` 内置全部运行模块、网页和脚本。删除旧 py、上传新版到原路径，完成后重启核心，新进程便使用整套新代码；不需要上传旁边的 `accounts.py`、`bot.py` 等维护源码。那些文件不被提取覆盖，单文件模式也不读取它们。

| 对象 | 缺失时 | 已存在时 |
|---|---|---|
| `.env` | **不生成**，程序拒绝启动并提示复制 `.env.example` | 只读：缺键补内存，文件不动；网页保存时才写回 |
| `data/data.db` | 自动创建数据库、表和索引 | 按新版 schema 补表、列和索引，不重置账号、Token、设置或会话 |
| 内嵌模块、网页、脚本 | 已在单文件里，不生成旁边源码 | 重启后整套替换为上传版本 |
| 成功快照 | 正常启动就绪后创建 | 记录当前就绪的程序，不替代配置和数据库 |

登录数据在 SQLite，不在 py。未过期且未撤销的网页会话可继续使用；Token 仍可能被官方独立撤销。保留原 `.env`、数据路径与目录，是更新保数据的前提。

管理员页“重启核心”执行：

1. 固定上传源码；语法及内嵌模块校验后，在配置/数据库副本中跑 `main()` 初始化。
2. 排空请求、Bot/网页任务、通知和连接；收尾失败取消更新，恢复旧核心。
3. SQLite backup 保存含 WAL 提交的数据库，配置按原字节备份。
4. 同 PID 启动候选，监听和核心线程准备好后提交成功标记，才允许业务执行。
5. 候选在就绪前抛错时，在干净 Python 中恢复旧源码、配置与数据库。

删除入口时仍在运行的服务可从成功快照恢复缺失文件；首次返回 400 并要求再次重启。成功快照位于 `.env` 所在目录，外置配置同样适用。语法错误仍拒绝切换，不自动覆盖坏上传。

**边界**：初始化预检不发真实官方请求；不能把线程创建成功当成授权/签到成功。不可捕获的退出或被杀进程不保证即时回滚；存在 pending 时，下次能启动入口会先恢复。只支持标准库单文件完整回滚，不把任意上传代码当沙箱。详见 [质量核查](QUALITY_REVIEW.md)。

保留数据的前提是入口仍在同一路径，`.env` / `EMOS_ENV_FILE` / `DATA_DIR` 和容器持久卷不被删除或改变。只删程序文件，不删 `config/`、`data/` 或 SQLite 的辅助文件。上传完成前不要重启核心；这段空窗若宿主进程被终止，必须先补齐入口才能重新启动。

容器里的实际操作：

```sh
docker exec emoskata rm /app/runtime/emoskata.py
docker cp emoskata.py emoskata:/app/runtime/emoskata.py
# 然后在管理员页点「重启核心」
```

先删旧文件再上传，是为了避免半写状态被当成新版本。

## 资源占用

- 稳态一个 Python 应用进程、单文件入口；重启预检期间临时创建隔离初始化子进程。
- 总物理 SQLite 连接上限 8，空闲缓存上限 3；创建、关闭中的连接也计入总容量。`cache_size=-256` 是每连接约 256 KiB 页缓存。
- Bot 工作线程按需创建，容量受 `BOT_WORKERS` / `BOT_MAX_JOBS` 限制；网页签到单 worker，调度单线程。
- 调度检查先读本地库；需要补签时才发官方请求，不能概括为“空闲永远不请求”。
- 每个账号的 `logs` 保留最近 200 条。零散登录失败记录保留 24 小时，在启动及每 64 次失败时清理；有效锁定不提前删除。
- 本轮隔离 60 秒样本：面板空闲约 0.033%–0.05% 单核，认证后初始 RSS 约 22 MiB、峰值约 38 MiB；实际本地 Bot 长轮询约 0.033%–0.05%、初始 RSS 约 23.5 MiB、5 线程。Android 期间会回收驻留页，后续 RSS 下降不代表优化效果；这些不是生产上限。完整样本见 [质量核查](QUALITY_REVIEW.md)。

## 补签与故障处理

- 错过某一分钟也会处理当天未完成账号，不要求恰好撞到那个分钟。
- 断网单独归类 `network`，不烧退避配额；纯网络失败不发失败通知。
- 401 或明确失效 → 停用账号；CF 403 HTML 不误判成密钥失效。
- 200 必须有有效 JSON 与签到字段，HTML/空壳不算成功。
- 只有读取请求在官方域间降级；签到等写请求遇 network/blocked 不立即换域重发，下次调度先读当天状态。自定义地址不追加官方站；emos 重定向不携带密钥跟随。

## 测试

```sh
python3 -B -m unittest discover -s . -p 'test_*.py'
python3 -B build_single.py
python3 -B check_web_js.py
```

测试只用临时 `.env`、临时数据库、本地 mock 服务和假 Telegram。**不是生产 TG / 真实 emos 账号的实机授权验收。**

## 许可

MIT，见 LICENSE。
