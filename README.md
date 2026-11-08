# emoskata

**本人专用 TG Bot：首个账号官方授权，后续 emos Token 添加，全部本人账号统一签到。**

Python 3.10+，零第三方 Python 依赖。可以上传生成的 `emoskata.py`，也可以上传分模块源码并启动 `app.py`。调度、Bot 和网页服务在一个应用进程内运行；重启预检会临时创建子进程。

> 当前版本已按单人方案接通 Token 引导、全部本人账号操作、余额展示和个人网页权限。历史跨 TG 共享数据保留但入口关闭。重启仍是导入预检，不是完整启动回滚保护；见 [验证记录](VALIDATION.md) 和 [维护地图](MAINTENANCE.md)。

## 配置：只有 `.env` 一个运行来源

运行配置写在 `.env`（默认入口文件同目录）。**只在首次创建文件时**继承显式提供的同名进程环境变量；之后以 `.env` 为准。`EMOS_ENV_FILE` 只选择配置路径，不充当第二套业务配置。

首次启动自动补全 `.env`（权限 0600，缺少管理员密码时生成随机值）。新数据库首次启动会打印初始密码；已有数据库重新生成配置的情况不能依赖日志取回密码。配置可在管理员网页保存，也可直接编辑文件；标为“重启生效”的项需重启，其余在后续读取时更新。

| 键 | 默认 | 说明 |
|---|---|---|
| `PANEL_PASSWORD` | 首次随机生成 | 管理员密码，至少 8 字符 |
| `HTTP_ENABLED` | `1` | `0` 关闭网页面板，只留 Bot 与调度（此时必须配 `TG_BOT_TOKEN`） |
| `HTTP_HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8080` | 监听端口 |
| `DATA_DIR` | `./data` | SQLite 数据目录 |
| `TG_BOT_TOKEN` | 空 | BotFather 签发的 Token |
| `TG_ALLOWED_USER_IDS` | 空 | 历史变量名沿用，但现在**只填一个本人正整数 TG ID**；同时为通知接收人。空值关闭 Bot/个人网页入口，多 ID 拒绝配置 |
| `TG_API_BASE_URL` | `https://api.telegram.org` | Telegram API 地址 |
| `EMOS_BASE_URL` | `https://emos.best` | emos API 地址；自定义站不向公共站降级 |
| `WEB_BASE_URL` | 空 | 个人网页公开地址（反代时填） |
| `PUBLIC_BASE` | 空 | 管理员网页公开地址 |
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

1. 私聊本 Bot 输入 `/login`，按提示填写**自己的 emos 用户 ID**。不是 TG 数字 ID，不是 Token。
2. Bot 调用 `getMe` 获得用户名，生成官方协议链接：

   ```text
   https://t.me/emospg_bot?start=link_[自己的emos用户ID]-[本Bot用户名]
   同意 → 本Bot?start=emosLinkAgree-[emosToken]
   拒绝 → 本Bot?start=emosLinkRefuse-[TelegramID]
   ```

3. 在 `@emospg_bot` 同意后返回。应用核验 `GET /api/user` 的账号 ID、本人 TG 绑定及待授权时限，事务内建立官方主账号证明。
4. 其他账号输入 `/token` 后粘贴该账号的**官方 emos Token**，或使用 `/token Token`。它们仍须绑定本人 TG，不接受他人的账号。
5. 所有登记的本人账号都能查看、切换、修改自己的签到设置；`/sign` 执行全部有效本人账号，无需建立关联关系。
6. 已有主账号后，`/login` 只重新授权该主账号。切换到其他账号也不会把它误当作主账号；更新其他账号 Token 使用 `/token`。

授权/输入 10 分钟有效。密钥消息尽力删除，删除失败不会重复添加；回复和日志不回显原始密钥。旧数据迁移不自动冒充“已完成官方首登”，需先官方重新授权。

### Telegram 的 `/` 命令菜单

`setMyCommands` 只为配置 owner 的私聊作用域注册；`setChatMenuButton` 把菜单按钮设为指令列表。权限仍在每次消息/回调中核对 `from.id` 和私聊 ID，菜单范围不是访问控制。

```text
/start /menu /login /token /sign /accounts
/settings /time /logs /web /cancel /help
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
- `/sign 任意文字`：本次签到带上你写的这句，**不改变**保存的模式，仅作用于当前选中账号；其他本人账号按各自保存模式执行。
- 每个本人账号分别保存模式（`sign_mode`）。
- 奖励由官方决定；随机字**不保证**多给萝卜，一切按官方返回记账。
- 内容上限 256 字、不允许控制字符（本程序的额外限制，官方文档未规定长度）。

判断今天是否已签：优先看 `GET /api/user` 返回体里的 `sign` 字段（官方原文：「签到信息 当天未签到时返回为 null」）。字段缺失时 fail-closed；`sign` 是空对象 `{}` **不算**已签（官方只说未签到返回 null，空对象不是已签证据，当成已签会白丢一天）。`429` **只有**在 `PUT /api/user/sign` 且 `message` 为「重复签到」时才算已签；其它 429 一律按限流退避。

`GET /api/sign/check` 的 `is_sign` 是**是否登录**（官方分类在「登录相关」），不能用来判断今天签没签。

## 全部账号、余额与个人网页

- `/accounts` 显示全部本人账号、官方 ID 和缓存余额。未知余额写“余额未知”，不拿 0 冒充；真实 0 正常显示。
- `carrot` 是官方可用余额；`last_carrot` 是最近单次所得。网页和管理表格分别标注，不把单次 +3 写成总余额 3。
- `/web` 发一次性入口链接。兑换、会话认证、设置、记录和签到逐次检查当前 owner；修改/清空 TG ID 后旧个人会话立即无权操作。
- 管理员网页依然用 `PANEL_PASSWORD`，只展示当前 owner 的账号与记录，不从其他 TG 保留数据中组装总览。
- `/link`、`/links`、`/code` 以及历史关联回调关闭；原 SQLite 链接、码、申请和账号不硬删除，调度忽略旧跨 TG 授权。
- 无官方主账号证明时禁止 Token 添加和自动签到。自动签到仅选择当前 owner 的有效本人账号。

## 部署

### 单文件（容器）

```sh
mkdir -p config data runtime
cp emoskata.py runtime/
printf 'TG_BOT_TOKEN="你的BotToken"\nTG_ALLOWED_USER_IDS="你的数字TGID"\n' > config/.env
docker compose up -d
```

`docker-compose.yml` 已配好三个挂载：

| 目录 | 内容 |
|---|---|
| `config/` | `.env`（唯一配置） |
| `data/` | `data.db`（本人账号、官方主号证明、会话、日志及保留历史） |
| `runtime/` | `emoskata.py` |

`prepare_docker.sh` 只在文件不存在时复制，不覆盖已有 `.env` 和已有 `runtime/emoskata.py`。

### 纯 Bot（不开网页）

在**已有 `.env` 中只修改** `HTTP_ENABLED=0`，保留原密码、Token、TG ID 和 `DATA_DIR`；不要用重定向覆盖整份配置。配置好以后启动：

```sh
EMOS_ENV_FILE=/app/config/.env python3 /app/runtime/emoskata.py
```

`HTTP_ENABLED` 应写进 `.env`；进程环境仅在首次创建文件时继承，之后不再用于覆盖运行配置。没有唯一 TG ID 不启动 Bot；启动校验还会拒绝“关闭网页且没有 Bot Token”的组合。纯 Bot 模式目前没有等价的 Bot 重启命令。

### 分文件

```sh
python3 app.py
```

## 更新：只替换 py

`app.py` 的软重启（管理员页“重启核心”）执行：

1. 检查语法和内嵌模块，在子进程以 `__name__='__probe__'` 做**导入预检**；复制源码和 `.env`，不运行 `main()`。
2. 排空当前请求及 Bot/网页作业；收尾失败时尝试恢复当前核心。
3. `exec` 自身，成功切换时 PID 不变；正常更新保留已有配置和业务数据。

**限制**：导入成功不能证明监听地址可绑定、目标数据库可迁移或 `main()` 能启动；也没有自动回滚。分模块 `app.py` 的依赖搜索路径已修复并有回归覆盖。固定配置下的两次单文件更新已验证保留账号、主号证明、余额、设置、会话与日志；详见 [独立质量核查](QUALITY_REVIEW.md)。

保留数据的前提是入口仍在同一路径，`.env` / `EMOS_ENV_FILE` / `DATA_DIR` 和容器持久卷不被删除或改变。只删程序文件，不删 `config/`、`data/` 或 SQLite 的辅助文件。上传完成前不要重启核心；这段空窗若宿主进程被终止，必须先补齐入口才能重新启动。

容器里的实际操作：

```sh
docker exec emoskata rm /app/runtime/emoskata.py
docker cp emoskata.py emoskata:/app/runtime/emoskata.py
# 然后在管理员页点「重启核心」
```

先删旧文件再上传，是为了避免半写状态被当成新版本。

## 资源占用

- 稳态一个 Python 应用进程、单文件入口；重启期间有临时导入预检进程。
- 最多缓存 3 个空闲 SQLite 连接；活跃连接未由此封顶。`cache_size=-256` 是每连接约 256 KiB 页缓存。
- Bot 工作线程按需创建，容量受 `BOT_WORKERS` / `BOT_MAX_JOBS` 限制；网页签到单 worker，调度单线程。
- 调度检查先读本地库；需要补签时才发官方请求，不能概括为“空闲永远不请求”。
- 每个账号的 `logs` 保留最近 200 条。零散登录失败记录保留 24 小时，在启动及每 64 次失败时清理；有效锁定不提前删除。
- 本轮隔离采样约 247 秒：空闲 CPU 约单核 0.1%，RSS 样本 4.67–8.76 MiB，记录峰值约 36.80 MiB，线程 5。条件是空库、无 Bot Token；不是实际 Bot 或满并发上限。前后未测出显著资源降幅，完整样本见 [质量核查](QUALITY_REVIEW.md)。

## 补签与故障处理

- 错过某一分钟也会处理当天未完成账号，不要求恰好撞到那个分钟。
- 断网单独归类 `network`，不烧退避配额；纯网络失败不发失败通知。
- 401 或明确失效 → 停用账号；CF 403 HTML 不误判成密钥失效。
- 200 必须有有效 JSON 与签到字段，HTML/空壳不算成功。
- 仅两个官方域互为备用；自定义地址不追加官方站；重定向不携带密钥跟随。

## 测试

```sh
python3 -B -m unittest discover -s . -p 'test_*.py'
python3 -B build_single.py
python3 -B check_web_js.py
```

测试只用临时 `.env`、临时数据库、本地 mock 服务和假 Telegram。**不是生产 TG / 真实 emos 账号的实机授权验收。**

## 许可

MIT，见 LICENSE。
