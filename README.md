# emoskata

emos 服（emos.best）自动签到 —— 网页面板 + Telegram Bot 双向控制，多账号管理。

**零第三方依赖**，只用 Python 3 标准库。容器镜像约 60MB。

## 为什么是 Python

`sqlite3` / `http.server` / `urllib` / `zoneinfo` / `threading` 全在标准库，`pip install` 一行都不用跑。Node 要实现同样功能得装 express + better-sqlite3（需编译原生模块）+ node-cron。

## 功能

- **网页面板** — 账号增删、单号/全量签到、实时状态、萝卜统计、日志、配置
- **Telegram Bot** — `/sign` `/status` `/add` `/del` `/logs` `/notify`，inline 按钮操作
- **emos 官方 OAuth** — 跳官方授权页，同意后密钥自动入库，不用手动复制
- **定时签到** — 默认每天 08:00，时区可配
- **失败退避重试** — 指数退避，不会无脑重刷
- **通知** — TG / Bark，成功失败分开开关

## 快速开始

### Docker

```bash
docker compose up -d
```

### 直接跑

```bash
python3 app.py
```

首次启动会打印随机面板密码，**只显示一次**。也可以自己指定：

```bash
PANEL_PASSWORD=你的密码 python3 app.py
```

打开 http://localhost:8080

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `8080` | 监听端口 |
| `PANEL_PASSWORD` | 随机生成 | 面板密码 |
| `PUBLIC_BASE` | 自动推断 | 对外访问地址，OAuth 回调要用。反代后填 `https://你的域名` |
| `DATA_DIR` | `./data` | 数据目录 |

## 添加账号

**方式一（推荐）**：面板点「用 emos 账号授权登录」，跳 emos 官方授权页，同意后自动添加。

> 需要面板能被公网访问（或已配好反代），否则 emos 的回调打不回来。本地测试用 `PUBLIC_BASE` 指定可访问地址。

**方式二**：从 emos 主站「欢迎」弹窗复制密钥（形如 `11_AkJO2UdOKinqmKOU`），粘贴到面板。

**方式三**：TG Bot 里发 `/add 11_你的密钥 备注名`

## Telegram Bot

1. 找 [@BotFather](https://t.me/BotFather) 建 bot，拿 token
2. 面板「通知设置」里填 token，保存
3. TG 里给你的 bot 发 `/id`，把返回的 chat_id 填进面板
4. 之后 `/sign` 就能直接签到

Bot 会自动启停：改了 token 保存后 15 秒内生效，不用重启容器。

## 接口契约

签到走 emos 官方开放 API：

```
GET  /api/sign/check          → {"is_sign": bool}
PUT  /api/user/sign?content=  → {"earn_point", "continuous_days", "sign_index"}
GET  /api/user                → 用户信息，401 表示 token 失效
PUT  /api/user/resetToken     → 换新密钥
```

**关键行为**：

- `/api/sign/check` 在**无 token 时也返回 200**（`{"is_sign":false,"user_id":null}`），所以不能拿它当 token 有效性的唯一判据 —— 必须配合 `/api/user` 看 401。
- `409` / `429` = 今天已经签过，视为成功跳过。
- `400` = 留言不合规，换一句重试。
- 官方要求：**禁止接口轮询**，**禁止选 0 点签到**。本项目的定时器强制最小 01:00。

## 自测

```bash
python3 selftest.py
```

覆盖时间钳制、附言池、域名降级顺序、无效 token 分支。

## 许可

MIT
