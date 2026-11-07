# 本轮实现与验收记录

## 实现目标

Telegram Bot 作主入口。登录调用 `@emospg_bot`，在本 Bot 登记验证后的 emos 官方账号 ID 与归属；当前账号用目标的官方 ID + 一次性码获得 **单向、直接、可撤销** 的签到关联。

账号数据保留在持久化 SQLite。生成的 `emoskata.py` 包含四个隔离的模块，部署仅需一个文件、一个 Python 进程。

## 主审实际执行

```sh
python3 -B build_single.py
python3 -B -m unittest discover -q
sh -n install.sh
git diff --check
```

本轮当前结果：**148 个 unittest，通过**。

- 账号层：47 项，含旧库增量迁移、原子关联码消费与竞争、归属保护、一跳权限与双方撤销。
- Bot：47 项，含官方回传、私聊身份、异步处理、通知按钮、checkpoint、启动失败重试。
- 客户端：17 项，含 CF 403、200 HTML、429 限流、官方域降级、自定义站不泄密、跨源重定向拒绝。
- 部署/HTTP：13 项，含同源写操作、请求体限制、密码轮换、凭据不回显、空环境值不覆盖数据库配置、单文件真实完整业务流程。
- 主审运行时集成：24 项，含实际 Bot + AccountService + 客户端 + 本地 HTTP；覆盖 A/B/C 登录、B 发码、A 关联、A+B 签到、不递归到 C、双方撤销、请求前撤销、补签退避和取消中的登录。

另已在一个**只有 `emoskata.py` 的临时目录**启动独立 Python 进程：`/api/health` 200、测试管理密码登录 200、SIGTERM 后退出码 0，无 traceback。此项不依赖旁边存在其他源码。

## 独立审查与主审复核边界

- `subaudit/@WorkBuddy·国内版` 完成了 Bot/账号层独立审查，给出了真实账户服务 + 假 Telegram/HTTP 的反例检查。主审另外读取实现并复跑正式测试。
- 其报告里未完成的 offset、guided-input 及中途撤销边界不视作通过。主审补了跨 Bot offset、验证中 `/cancel`、撤销后不请求目标/不发送已排队通知等回归，并修正实际实现。
- 主审另外修了旧请求不能覆盖重登后的新 token、根账号快照必须进入 runtime、空环境值覆盖管理页配置、重定向拒绝时关闭连接、配置错误不永久持有签到锁。
- 第二个后缀的模型调用返回空内容，以及失败/超时的审查任务，**均不计入完成或双审通过**。

## 不包含的验收

- 没有使用生产 TG Bot Token 或真实 emos 账号运行授权链；本地测试不是实机授权验收。
- 本环境没有 Docker daemon，未实际构建并运行 Docker 镜像。已经测试其实际入口文件独立运行。
- CI 新增 Python 3.10 / 3.12 标准工具回归。CI 结果在发布到 main 后才产生，当前不宣称 CI 已通过。
- 已经发出的远端 HTTP 请求不能追溯取消；撤销检查针对后续尚未发出的请求和后续信息展示。

## 官方契约来源

- [emos 官方机器人授权](https://wiki.emos.best/api/link.html)：`link_[应用创建者emos用户ID]-[Bot用户名]`；同意 `emosLinkAgree-[token]`，拒绝 `emosLinkRefuse-[TelegramID]`。
- [官方前端](https://github.com/somebyteorg/emos_home)：`web/views/home.vue` 真实使用 `/api/user` 的 `user_id` 与 `telegram_user_id`。
- [HTTP 429 规范](https://www.rfc-editor.org/rfc/rfc6585.html#section-4)：通用 429 表示限流，不能不看响应就算已签到。
- Python 自带 `urllib.request.HTTPRedirectHandler` 源码确认默认复制普通 header，因此本项目安装拒绝重定向的 handler 保护 Bearer。
