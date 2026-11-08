# 验收记录

## 本轮做了什么

1. **签到附言（随机字）**：官方 `PUT /api/user/sign?content` 的 `content` 是 query 参数，官方描述为「说点什么进行随机签到以获得更多萝卜」。已实现每账号 `sign_mode`（`normal` / `random`），`/sign 任意文字` 单次带附言，自动签到按各自模式执行。
2. **官方接口语义纠正**：`429` 只有 `PUT /api/user/sign` + `message=="重复签到"` 才算已签；`GET /api/sign/check` 的 `is_sign` 是「是否登录」不是「今天签没签」；今天是否已签优先读 `GET /api/user` 的 `sign` 字段，缺失时 fail-closed。
3. **登录改为每用户自己的 emos ID**：删除运营者固定 `EMOS_APP_USER_ID`。官方链接里那个 ID 就是「自己的用户ID」。
4. **关联需目标本人二次确认**：`link_requests` 表 + `pending_links` / `review_link` / `withdraw_link`，Bot 与网页都有确认入口。
5. **本人登录必须验明身份**：`finish_login` 要求 `telegram_user_id` 非空且等于当前 TG 用户。
6. **无 root 软重启**：`_prepare_core` / `_exec_core` / `_drain_core` / `_resume_core` / `_stop_core` + `request_idle` 屏障，PID 不变，坏文件拒绝重启。
7. **配置缓存**：`ConfigStore.app_config()` 用 `.env` 的 stat 指纹判断是否需要重新解析，命中缓存时约 0.2 ms/次。
8. **日志上限**：每账号 `logs` 只保留最近 200 条。
9. **冗余清理**：删除未调用的 `_status_of`、`drop_all_sessions`、`Store.run_all` + 模块级 `run_all`、`EmosError.payload`；`pyflakes` 全项目零告警。
10. **签到请求不再换域名重发**：`PUT /api/user/sign` 不是幂等请求，网络中断时请求可能已经到达官方并签到成功，换域名重发等于二次签到尝试（官方明确禁止接口轮询）。现在只有网关拦截（403/HTML，确定未执行）才换备用域名；网络错误直接报 `network`，交给本地退避。
11. **`sign: {}` 不再当成已签**：官方只写「当天未签到时返回 null」。空对象不是已签证据，当成已签会让当天调度全程跳过，白丢一天。
12. **`重复签到` 判定归一**：429 判定与「已签到」判定共用 `_already_message()`，带句号、尾随空格、全角空格、近义写法的都能识别，不再被误判成限流。
13. **空轮询退避**：`getUpdates` 立即返回空（本地 Bot API、反代提前收连接、网络抖动）时，原来固定 `wait(0.1)` 会退化成 9.25 次/秒短轮询（实测 3.10% CPU）。现在连续空返回 >3 次就指数退避到 3 秒；正常长轮询链路不受影响（实测 0.05 次/秒、0.15% CPU）。
14. **签到时间钳制去重**：`accounts._clamp_time` 改为复用 `emos.clamp_sign_time`，删掉第二份实现。
15. **状态文案单一来源**：`webui` 的「状态→中文」原本在服务端两处 + 内嵌 JS 一处各写一遍且已经不一致，现统一为模块级 `_STATUS_TEXT`/`_OTHER_TEXT` + `status_text()`，JS 通过 `__STATUS_JS__` 注入同一份数据。
16. **状态文案注入纳入检查**：`check_web_js.py` 改为检查**渲染后**的脚本（占位符未替换时 `const labels=__STATUS_JS__` 语法合法但运行必崩，原来的 `node --check` 抓不到）。
17. **生产规则补真测试**：`test_bot` 用的 `FakeAccounts` 自己复刻了 `sign_mode` 校验，导致生产规则零覆盖（变异测试证实：删掉 `accounts.py` 的校验，`test_bot`/`test_accounts` 全绿）。新增 `test_accounts.AccountCase.test_set_account_sign_mode_whitelist_and_persistence`，用变异测试确认能抓到删除。
18. **替身签名守卫**：`test_bot` 的 `FakeAccounts` 复刻了 27 个生产方法，签名漂移会让 Bot 层测试假通过（实测生产 `toggle_link` 已多出 `enabled`，替身没跟上）。新增 `test_bot_fake_matches_production_signatures` 双向校验：同名方法签名必须一致，且生产新增的公开异步方法不能缺席。变异测试确认：给生产加尾部参数 → 失败；给生产加新公开方法 → 失败。
19. **单文件漂移门禁**：`build_single.py --check` 只比较不写，CI 用它拦住「磁盘模块改了但 `emoskata.py` 没重建」（Docker 只拷 `emoskata.py`，这种漂移会让镜像跑旧代码）。实测把 `emos.py` 改回旧写法后 `--check` 返回 exit=1。
20. **启动自检（真执行，不只是编译）**：`_prepare_core` 原来只做 `ast.parse` + `compile`，语法合法但一导入就崩的文件能一路过闸，`execv` 之后进程直接消失，服务再也起不来。现在在 `execv` 前用子进程真的 exec 一遍新 py（`__name__='__probe__'`，`main()` 不跑、端口不占），退出码非 0 就 400 拒绝切换，旧核心继续服务。源码走独立副本文件（子进程读共享 fd 会把 offset 带到末尾，execv 后新核心读到 0 字节当场死掉——这个坑真踩过）；`.env` 也走副本，避免「自检没过但配置已被新 schema 重写」。
21. **关掉自检失败的临时文件**：`_prepare_core` 里 `TemporaryFile` 在自检抛错时原样泄漏，`-W error::ResourceWarning` 抓到，已改为失败即 close。

## 主审实际执行

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
