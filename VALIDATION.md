# 验证记录

## 最新独立质量复验（2026-11-08，本地验收，未推送）

本轮以实际工作树为基线，保留登录流程，独立核查单文件运行、更新数据保留和资源占用。完整结论与边界见 [QUALITY_REVIEW.md](QUALITY_REVIEW.md)。

- 最终全量 **265 项通过，96.992 秒**；启用 `ResourceWarning` 检查。
- 空目录只有一个 `emoskata.py`，以 `python -I -S -u` 禁用外部包路径启动，HTTP 与登录正常。
- 两次真实 exec 更新后，`.env` 字节与账号、主号证明、余额、设置、日志、管理员/个人会话等表内容一致。
- 修复分模块入口预检缺依赖路径、零散登录失败记录长期积累，以及收尾期限耗尽时没有线程退出调度窗口的问题。
- 新质量测试 6 项，加独立无外部包启动测试 1 项；三组隔离副本变异均被对应用例捕获。
- 注释阶段单独对照：28 个源文件、7 个内嵌模块及 bootstrap 的可执行 AST 不变。pyflakes、四份真实网页 JS、shell 语法、漂移及 diff 检查通过。
- 约 247 秒空库/无 Bot Token 采样：单核 CPU 约 0.1%，RSS 样本 4.67–8.76 MiB，本进程记录峰值约 36.80 MiB，线程 5。没有测出显著空闲资源降幅。

当前单文件 283088 字节；md5 `88a80dad04b0ee7a3226cfd0489033cc`；sha256 `05507bf34ff20ee8ab93ef24bbf3aa0567453647a352b00c6caeefb9c37b0b14`。

仅在独立临时目录测试，未替换真实 Bot 的运行文件或用户配置；无 Docker daemon、多版本 CI 或本轮真实账号签到验收。账号退出仍是未实现功能；导入预检仍不等于 `main()` 健康验收或自动回滚。

## 历史单人版本验证（2026-11-08，后续已有上方质量修订）

| 验证 | 真实结果 |
|---|---|
| 完整 unittest | 258 项 OK，100.328 秒；包括官方首号、Token、单人范围与历史兼容 |
| 12 项斜杠命令 | `BotCommand` 字符/长度/唯一性全部符合官方文档；逐项调用真实 handler |
| InlineKeyboard | URL/callback 互斥、1–64 UTF-8 字节；所有现行按钮 action/参数都有回归 |
| 普通/随机与分页 | 有效模式和页码处理；非法模式、负页码、旧共享回调拒绝 |
| 真单文件完整流程 | localhost 官方替身：首号 OAuth→Token 添加→全部本人签到→网页→改 owner 失效 |
| 官方 Telegram 菜单 | 真实 `setMyCommands`/`getMyCommands` 回读 12 项，scope=owner 私聊 |
| 官方私聊菜单按钮 | `setChatMenuButton`/`getChatMenuButton` 回读 type=commands |
| 真实通知 | 已向配置 owner 发送连通测试并获 Telegram 成功响应 |
| 真实本地服务 | health 200、admin login 200、未认证 accounts 401、真实 getUpdates 多次成功 |
| 本人网页/通知/自动签到 | 6 项专门回归；改/清空 owner 后旧会话和队列任务拒绝，忽略旧跨 TG grant |
| 静态/构建 | pyflakes、4 个 Node JS 语法、shell 语法、漂移和 diff 检查通过 |

官方规则来源：
- https://core.telegram.org/bots/api#botcommand
- https://core.telegram.org/bots/api#botcommandscopechat
- https://core.telegram.org/bots/api#setmycommands
- https://core.telegram.org/bots/api#menubuttoncommands
- https://wiki.emos.best/api/link.html

当时的 `emoskata.py` 为 281498 字节；md5 `f3acfa371bd21047184f6816c3d90915`，sha256 `653e38213f821c014aa76771785b03073b08e023eb76ed1766f82275df64b2ff`。此指纹属于历史单人版本，不是上方最新质量修订。

真实网络启动使用独立 `.env` / 空 SQLite，未覆盖已有数据；报告与日志脱敏。真实账号的官方授权/签到、Android 长时间后台存活和生产 Docker 尚未验收。应用导入预检仍不是完整 `main()` 健康门禁或自动回滚。


## 接手复验与已发现缺口（2026-11-08）

接手基线 `50fe5dd` 的 231 项测试独立复跑通过。它们没有覆盖领导后来提出的完整 token 入口、全部账号操作、关联余额展示，也没有证明 `main()` 的真实启动检查。详细对账和复验命令见 [TAKEOVER_AUDIT.md](TAKEOVER_AUDIT.md)。

本轮只重构注释和说明；源文件去文档串的可执行 AST 与基线一致，生成模板只改头部文档串。部署脚本/CI 的非注释内容也与基线一致。本地源码测试、脚本检查和特定演练均不等于生产环境验收。

实际复跑：`unittest` 231 项 OK；兼容入口 `selftest.py` 在重试后 231 项 OK；Node 编译四份真实网页脚本、pyflakes、shell 语法与漂移检查全部通过。首轮兼容自检的 120 秒超时没有计作通过。重建 `emoskata.py` 270275 字节，详细哈希及缺口演练记录见接手报告。

## 历史实现记录：截至 50fe5dd

下面保留之前实现过的项目作为历史线索。早期登录方案不代表最新 token 多账号需求已经完成；历史采样及一次成功演练也不代表普遍安全保证。

1. **签到附言（随机字）**：官方 `PUT /api/user/sign?content` 的 `content` 是 query 参数，官方描述为「说点什么进行随机签到以获得更多萝卜」。已实现每账号 `sign_mode`（`normal` / `random`），`/sign 任意文字` 单次带附言，自动签到按各自模式执行。
2. **官方接口语义纠正**：`429` 只有 `PUT /api/user/sign` + `message=="重复签到"` 才算已签；`GET /api/sign/check` 的 `is_sign` 是「是否登录」不是「今天签没签」；今天是否已签优先读 `GET /api/user` 的 `sign` 字段，缺失时 fail-closed。
3. **登录改为每用户自己的 emos ID**：删除运营者固定 `EMOS_APP_USER_ID`。官方链接里那个 ID 就是「自己的用户ID」。
4. **关联需目标本人二次确认**：`link_requests` 表 + `pending_aw_link`，Bot 与网页都有确认入口。
5. **本人登录必须验明身份**：`finish_login` 要求 `telegram_user_id` 非空且等于当前 TG 用户。
6. **无 root 软重启**：`_prepare_core` / `_exec_core` / `_drain_core` / `_resume_core` / `_stop_core` + `request_idle` 屏障，PID 不变，坏文件拒绝重启。
7. **配置缓存**：`ConfigStore.app_config()` 用 `.env` 的 stat 指纹判断是否需要重新解析，命中缓存时约 0.2 ms/次。
8. **日志上限**：每账号 `logs` 只保留最近 200 条。
9. **冗余清理**：删除未调用的 `_status_of`、`drop_all_sessions`、`Store.run_all` + 模块级 `run_all`、`EmosError.payload`；`pyflakes` 全项目零告警。
10. **签到写请求不跨域补发**：`call()` 中 `may_retry_gateway` 只对 GET 生效；PUT 的网络错误返回 `network`，不会改用备用域名补发。这不代表所有手动/调度操作具备幂等保证。
11. **`sign: {}` 不再当成已签**：官方只写「当天未签到时返回 null」。空对象不是已签证据，当成已签会让当天调度全程跳过，白丢一天。
12. **`重复签到` 判定归一**：429 判定与「已签到」判定共用 `_already_message()`，带句号、尾随空格、全角空格、近义写法的都能识别，不再被误判成限流。
13. **空轮询退避**：连续立即空返回超过 3 次后，`wait(min(3.0, 0.2 * empty_runs))` 线性增加等待、上限 3 秒，不是指数退避。历史短轮询与长轮询样本属于当时的本地测量，不作为生产性能保证。
14. **签到时间钳制去重**：`accounts._clamp_time` 改为复用 `emos.clamp_sign_time`，删掉第二份实现。
15. **状态文案单一来源**：`webui` 的「状态→中文」原本在服务端两处 + 内嵌 JS 一处各写一遍且已经不一致，现统一为模块级 `_STATUS_TEXT`/`_OTHER_TEXT` + `status_text()`，JS 通过 `__STATUS_JS__` 注入同一份数据。
16. **状态文案注入纳入检查**：`check_web_js.py` 改为检查**渲染后**的脚本（占位符未替换时 `const labels=__STATUS_JS__` 语法合法但运行必崩，原来的 `node --check` 抓不到）。
17. **生产规则补真测试**：`test_bot` 用的 `FakeAccounts` 自己复刻了 `sign_mode` 校验，导致生产规则零覆盖（变异测试证实：删掉 `accounts.py` 的校验，`test_bot`/`test_accounts` 全绿）。新增 `test_accounts.AccountCase.test_set_account_sign_mode_whitelist_and_persistence`，用变异测试确认能抓到删除。
18. **替身签名守卫**：`test_bot` 的 `FakeAccounts` 复刻了 27 个生产方法，签名漂移会让 Bot 层测试假通过（实测生产 `toggle_link` 已多出 `enabled`，替身没跟上）。新增 `test_bot_fake_matches_production_signatures` 双向校验：同名方法签名必须一致，且生产新增的公开异步方法不能缺席。变异测试确认：给生产加尾部参数 → 失败；给生产加新公开方法 → 失败。
19. **单文件漂移门禁**：`build_single.py --check` 只比较不写，CI 用它拦住「磁盘模块改了但 `emoskata.py` 没重建」（Docker 只拷 `emoskata.py`，这种漂移会让镜像跑旧代码）。实测把 `emos.py` 改回旧写法后 `--check` 返回 exit=1。
20. **重启前导入预检**：子进程以 `__name__='__probe__'` 执行待更新 py，拦住测试所覆盖的顶层导入异常；源码和 `.env` 使用副本，失败时关闭临时镜像。它不运行 `main()`，不能验证绑定、SQLite 迁移、纯 Bot 组合，也没有自动回滚到旧核心。绝对 `DATA_DIR` 和任意顶层副作用没有完整隔离。
21. **关掉自检失败的临时文件**：`_prepare_core` 里 `TemporaryFile` 在自检抛错时原样泄漏，`-W error::ResourceWarning` 抓到，已改为失败即 close。

## 历史执行结果（50fe5dd，不是本次重构结果）

```sh
python3 -B -m unittest discover -s . -p 'test_*.py'   # 231 项，OK
python3 -B build_single.py                            # 270651 字节，7 个隔离模块
python3 -B build_single.py --check                    # 单文件与磁盘模块一致（CI 门禁）
python3 -B check_web_js.py                            # 内嵌 JS 语法通过
python3 -m pyflakes *.py                              # 无输出
sh -n install.sh && sh -n prepare_docker.sh
```

单文件独立进程实测（`/tmp/sm14`，只有 `emoskata.py` + `data/`）：

| 项 | 结果 |
|---|---|
| 启动 | 打印监听地址，生成 `.env` 与随机管理员密码 |
| `GET /api/health` | 200 `{"ok": true, ...}` |
| `GET /login` | 200 |
| 进程数 | 1 |
| RSS | ≈ 20 MB（VmHWM 37 MB） |
| 线程 | 4 |
| 空闲 20 秒 CPU | 1 tick（≈ 0.01 秒） |

## 已用真实请求验证的请求形状

`test_client.py` 里的本地 mock 记录每次 `PUT` 的完整 path 与 body：

| 模式 | 期望 | 实测 |
|---|---|---|
| `sign_once()` | path 无 query，body 空 | `('/api/user/sign', b'')` |
| `sign_once(mode='random')` | path 带 `content=`，值来自词表 | `content` ∈ `CONTENTS` |
| `sign_once(' 今天也在 ')` | 原样带出，去首尾空格 | `content=今天也在` |
| `sign_once(mode='random2')` | 发请求前就拒绝 | `status == 'config'`，`server.hits == []` |

`test_runtime.py` 另验了自动签到：`sign_mode='random'` 的账号在 `scheduler_tick()` 时请求带 `content=`，`normal` 账号 body 为空。

## 独立审查

由子代理 `代码审查员`（官方接口对接）与 `外科审查员`（资源与冗余）分别执行，主审逐条复核。**子审结论不直接采信**，验不过的退回。超时或空输出的一律不计通过。

## 不包含的验收

- **没有**用生产 TG Bot Token 或真实 emos 账号跑过完整授权链。本地 mock 不是实机授权验收。
- 本环境没有 Docker daemon，**未**实际构建运行 Docker 镜像；只验证了镜像的入口文件能独立运行。
- 官方站点在沙箱里不可达（`curl https://emos.best` 返回 000），因此所有官方行为都来自 Postman 集合与 wiki 原文，不是现场实测响应。
- 已发出的 HTTP 请求无法追溯取消；撤销只影响后续请求与后续展示。
