# emoskata

**TG Bot 主入口 · emos 官方 Bot 登录 · 账号之间的签到关联 · 普通/随机字签到。**

Python 3.10+，零第三方 Python 依赖。可以只上传生成的 `emoskata.py`，也可以上传分模块源码并只启动 `app.py`。调度、Bot 与网页面板在**一个**应用进程里运行。

## 配置：只有 `.env` 一个来源

所有配置都写在 `.env`（默认入口文件同目录）。程序**不读取进程环境变量做业务配置**，唯一的例外是 `EMOS_ENV_FILE`，它只用来指定 `.env` 的路径。

`.env` 第一次启动时自动生成（权限 0600，含随机管理员密码，日志里只显示一次）。之后改配置的方式：网页面板保存，或直接编辑 `.env`（下次读取时自动生效，无需重启；标了「重启生效」的项除外）。

| 键 | 默认 | 说明 |
|---|---|---|
| `PANEL_PASSWORD` | 首次随机生成 | 管理员密码，至少 8 字符 |
| `HTTP_ENABLED` | `1` | `0` 关闭网页面板，只留 Bot 与调度（此时必须配 `TG_BOT_TOKEN`） |
| `HTTP_HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8080` | 监听端口 |
| `DATA_DIR` | `./data` | SQLite 数据目录 |
| `TG_BOT_TOKEN` | 空 | BotFather 签发的 Token |
| `TG_ALLOWED_USER_IDS` | 空 | 只允许这些 TG 用户 ID 使用 Bot，逗号分隔。**留空 = 任何人搜到都能用**，自己用请填上自己的 ID |
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

## 登录：走 emos 官方 Bot 授权

官方 wiki（`wiki.emos.best` 的「连接 → 机器人授权」）原文：

```text
https://t.me/emospg_bot?start=link_[自己的用户ID]-[机器人的name]
同意 → https://t.me/[机器人的name]?start=emosLinkAgree-[用户token]
拒绝 → https://t.me/[机器人的name]?start=emosLinkRefuse-[TelegramID]
```

流程：

1. 在 Bot 发 `/login`，Bot 引导填写**自己的 emos 用户 ID**。
   - 获取方式：登录 `https://emos.best/` → 点首页账号卡片左上角的「欢迎」/称号 → 弹窗显示「用户ID：e……」，点「点击复制用户ID」。不是「当前密钥」，不需要重置密钥。
2. Bot 用 `getMe` 拿到自己的用户名，拼出官方链接给你点。
3. 在 `@emospg_bot` 同意或拒绝，都会回到本 Bot。
4. 同意后 Bot 拿到 token，用 `GET /api/user` 校验并读出官方账号 ID，登记归属；`telegram_user_id` 必须与发起人一致，否则拒收。
5. 授权请求 10 分钟有效。回传密钥的消息会被删除，密钥不进日志、不回显。

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
- `/sign 任意文字`：本次签到带上你写的这句，**不改变**保存的模式，也不影响关联目标的模式。
- 每个账号自己保存模式（`sign_mode`），关联方不能替它改。
- 奖励由官方决定；随机字**不保证**多给萝卜，一切按官方返回记账。
- 内容上限 256 字、不允许控制字符（本程序的额外限制，官方文档未规定长度）。

判断今天是否已签：优先看 `GET /api/user` 返回体里的 `sign` 字段（官方原文：「签到信息 当天未签到时返回为 null」）。字段缺失时 fail-closed；`sign` 是空对象 `{}` **不算**已签（官方只说未签到返回 null，空对象不是已签证据，当成已签会白丢一天）。`429` **只有**在 `PUT /api/user/sign` 且 `message` 为「重复签到」时才算已签；其它 429 一律按限流退避。

`GET /api/sign/check` 的 `is_sign` 是**是否登录**（官方分类在「登录相关」），不能用来判断今天签没签。

## 账号之间的关联

前提：A、B 都已在**本 Bot** 通过官方授权登录。

1. B 进入关联管理点「生成码/链接」，或发 `/code`。
2. B 把 **官方账号 ID + 一次性关联码** 交给自己同意的代签方 A。
3. A 发 `/link B的官方账号ID 一次性关联码`。
4. **B 本人必须在关联菜单里确认**，确认前不能代签。
5. 确认后 A 点签到，会一起处理 A 与已开启联动的 B。

- ID 只用于定位目标，单独不能授权。
- 关联码随机生成，库里只存 SHA256，最多 10 分钟、只用一次；重新生成会使旧码失效。
- 只建立 **A → B** 单向直接权限。不自动反向，不把 B → C 转授给 A，不递归。
- A 可暂停/启用外向联动；A、B 都能解除。解除后不再请求目标，也不再显示目标记录。
- **官方没有「emos 账号互相关联」这种接口。** 这里说的「一起签到」是：用 A 的 token 签 A，用 B 的 token 签 B，各走各的 `PUT /api/user/sign`。不修改 emos 官方账号间关系，不需要交换或复制 token。

## 部署

### 单文件（容器）

```sh
mkdir -p config data runtime
cp emoskata.py runtime/
printf 'TG_BOT_TOKEN="你的Token"\n' > config/.env
docker compose up -d
```

`docker-compose.yml` 已配好三个挂载：

| 目录 | 内容 |
|---|---|
| `config/` | `.env`（唯一配置） |
| `data/` | `data.db`（账号、关联、日志） |
| `runtime/` | `emoskata.py` |

`prepare_docker.sh` 只在文件不存在时复制，不覆盖已有 `.env` 和已有 `runtime/emoskata.py`。

### 纯 Bot（不开网页）

```sh
printf 'HTTP_ENABLED=0\nTG_BOT_TOKEN="你的Token"\n' > config/.env
EMOS_ENV_FILE=/app/config/.env python3 emoskata.py
```

`HTTP_ENABLED` 必须写进 `.env`，不是进程环境变量——程序不读进程环境变量做业务配置，校验会拒绝「关了网页又没配 Bot Token」的配置。

### 分文件

```sh
python3 app.py
```

## 更新：只替换 py

`app.py` 的软重启（管理员页「重启核心」）会：

1. 预检新文件的语法（含内嵌模块），坏文件拒绝重启，继续跑旧代码。
2. 停止接收新请求，排空进行中的请求与 Bot 任务。
3. `exec` 自身，**PID 不变**。
4. `.env` 与 `data/data.db` 原样保留。

容器里的实际操作：

```sh
docker exec emoskata rm /app/runtime/emoskata.py
docker cp emoskata.py emoskata:/app/runtime/emoskata.py
# 然后在管理员页点「重启核心」
```

先删旧文件再上传，是为了避免半写状态被当成新版本。

## 资源占用

- 一个 Python 进程、一个入口文件。
- SQLite 连接池上限 3，`cache_size=-256`（256 KiB）。
- Bot 线程池固定 `BOT_WORKERS`，网页签到单 worker，调度单线程。
- 空闲时不轮询官方接口；调度只读本地库，间隔 `SCHEDULER_INTERVAL`。
- 每个账号的 `logs` 只保留最近 200 条。
- 实测（单文件、空库、无 Bot Token、空闲）：RSS ≈ 20 MB，线程 4，空闲 20 秒 CPU ≈ 0.01 秒。

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
