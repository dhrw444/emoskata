# 接手核查：需求与实现对账

> 本文保留需求演变及历史基线，不是最新代码质量结论。最新独立复验见 [QUALITY_REVIEW.md](QUALITY_REVIEW.md)：265 项回归通过；模块预检、过期失败记录清理及收尾调度窗口已修复，正常单文件更新的数据保留已补强验证。账号退出与完整启动回滚仍未实现；登录流程按 YG 决定保持不变。

日期：2026-11-08。接手基线：`main / 50fe5dd80c10cce1a86e110b130a7844eba75e9a`。

## 后续单人方案更新（本地验收，未推送）

YG 后来取消跨 TG 共享，确定：唯一 TG ID 既接收通知也校验操作；首个账号通过官方 Bot 授权，其他本人账号用 emos Token 添加。

本轮已经接通：
- `TG_ALLOWED_USER_IDS` 沿用变量名但只接受一个正整数；空/非法不开放 Bot 或个人网页。
- `primary_account_id` 只由已校验的官方 pending 授权建立；旧数据不自动伪造主号证明。
- 裸 `/token`、内联 Token、主号重授权、全部本人账号范围、Bot/网页余额展示及管理员总览。
- 个人网页会话、通知和调度逐次读 owner；历史共享数据保留、共享入口关闭。
- 12 项 Telegram `BotCommand` 已按官方规范注册在 owner 私聊 scope，并通过 `getMyCommands` 回读；`MenuButtonCommands` 已回读。

全量 258 项测试通过；真实 Bot 长轮询已成功，健康/登录 200，无认证账号接口 401。正式 emos 首次授权仍需 YG 在 `@emospg_bot` 同意，尚不宣称真实账号签到验收。下面矩阵及缺口是 **50fe5dd 历史基线**，不能用旧结果否定已修链路，也不代表剩余重启/资源缺口已修复。


**结论：不能认定“所有需求已经完成”。原有 231 项测试通过，但没有覆盖数个最新产品要求及真实启动边界。** 本轮只重构注释、整理说明及补充复验资料，没有修改业务代码，没有推送。

## 判据和范围

- 从 emoskata 历史会话提取直接发言，排除工具伪消息和嵌入的子审报告，按时间顺序判断需求。后来的“改用官方 emos token 登录”优先于早期官方 Bot 登录方案。
- 主审通读 7 个业务模块、构建/资源工具、部署文件及与结论相关的测试；独立审查报告只当线索，主要问题再次用 localhost 和假官方响应复验。
- 官方来源：[emos wiki](https://wiki.emos.best/)、[开放接口](https://wiki.emos.best/api/use.html)、[授权登录](https://wiki.emos.best/api/link.html)、[萝卜机制](https://wiki.emos.best/introduction/carrot.html)。官方 wiki 指向 [Postman](https://www.postman.com/somebyteorg/emos)。本次重新抓取 wiki 源文并读取当前 Postman 集合元数据。
- YG 指定的 [emos.dpdns.org](https://emos.dpdns.org/) 首页自称补充官方 wiki 细节。它描述签到附言及随机萝卜机制，用作补充；不会用它代替第一手接口契约。
- 没有用生产 TG Bot Token 或真实 emos token 跑官方授权/签到。没有实际运行 Docker daemon 或验证 systemd。localhost/mock 结果不等于生产可用。

## 历史接手需求矩阵（50fe5dd）

| 领导要求 | 实际实现 | 判定及复验入口 |
|---|---|---|
| 纯 Python、尽量零依赖、容器一个入口 | `Dockerfile` 运行 `emoskata.py`；业务仅标准库；稳态单应用进程 | 已实现；重启导入预检有临时子进程，不能描述为永远只有一个进程 |
| 配置只用 `.env`，网页可改 | `ConfigStore` schema/原子保存/动态快照；仅首次创建继承环境；SQLite 保留业务状态 | 已实现主体；监听和数据路径等需重启，不能说所有字段立即生效 |
| TG 为主入口，个人网页与管理页分离 | Bot 私聊判权；个人一次性网页链接；两套会话/路由 | 已实现主体；纯 Bot 模式没有网页重启按钮对应的 Bot 重启命令 |
| 多账号换成输入官方 emos token | 内联 `/token <token>` 有路径；裸 `/token` 试图创建不被允许的 `token` 输入会话 | **不完整**：裸命令报“输入会话类型无效”，菜单/命令注册仍偏旧 OAuth 入口 |
| 一个入口管理全部账号、获得全部账号权限 | `owned()` 能列多账号；当前 `scope()` 仅选中根账号及其一跳关联；token 要求官方 TG 绑定等于当前 TG | **不完整**：添加两账号并不自动一起签；不同 TG 绑定的官方 token 被拒绝。报告不主张移除身份校验，修复须明确授权语义 |
| 关联账号的萝卜数量可见 | 数据层存 `carrot/carrot_at`；Bot 列表有显示；个人网页 `_basic()` 未透传余额 | **不完整**：个人网页关联目标只有 `last_carrot`，那是单次所得，不是余额 |
| 管理员能总览全部账号 | `admin_overview()` 存在；`app.py` 仍调用普通 `list_accounts()` 组装 API | **不完整**：总览方法未接入生产入口，不能靠方法存在认定已交付 |
| Bot 只允许指定 TG ID，只有本人能用 | `TG_ALLOWED_USER_IDS` 在 Bot 消息/回调准入生效 | 已实现 Bot 准入；空名单开放。名单更改不会自动吊销既有个人网页会话；管理员页仍由面板密码授权 |
| `/sign` 附言、每账号普通/随机字自动签到 | query `content`、每账号 `sign_mode`、Bot/网页模式切换和调度 | 已实现本地链路；随机字不保证收益，实际所得以官方 `earn_point` 为准；未做生产签到 |
| 无 root 重启核心；只删旧 py 后上传，数据不丢 | 固定配置下有效单文件更换与语法/导入异常拒绝有演练；使用 `execv` | **部分实现**：导入预检不跑 `main()`；端口不可绑定或目标 SQLite 不兼容仍可返回 202 后进程退出，无旧核心自动恢复 |
| 资源优化与清冗余 | 空轮询退避、每账号日志裁剪、部分表清理、空闲连接缓存、队列上限 | 有实现但不完整；活跃连接未封顶，单次失败 `until=0` 的限流记录未被现有清理条件回收 |

## 必须修的真实问题（按风险）

### P0 · 重启安全承诺超出实际预检范围

`_probe_core` 使用 `__name__='__probe__'`；`main()` 的数据库迁移、环境组合和 HTTP 绑定没有执行。独立复验：把 `HTTP_HOST` 改为本机没有的地址，保存返回 200，重启返回 202，然后进程 `exit=1`；切向不兼容 SQLite 同样退出。以前的“导入即崩被拒绝”演练只覆盖其特定场景。

另两项边界：模块入口 `app.py` 自检副本目录没有依赖模块，合法代码也被 `ModuleNotFoundError` 拒绝；直接换回旧 py 不保证可降级，新 schema 写进真实 `.env` 后旧程序会拒绝未知键。

### P1 · 最新 token 入口没有贯通

裸 `/token` 失败；内联命令能走添加流程，但入口说明、注册和键盘没有统一。按旧 `/login <参数>` 的路径输入形似 ID 的 token，会把该值拼进官方 Bot deep link。内联 token 消息未删除；短数字也绕过当前消息删除条件。本轮仅记录这些行为，没有把敏感值写入报告。

### P1 · “管理全部账号”仍是当前账号模型

两个自己的账号同时存在时，`scope()` 与 `visible()` 默认仍只有当前选中的账号。其他账号需要手动切换或既有一跳关联。不同 TG 绑定 token 被拒绝是实际本地身份限制，不是领导要求的已经完成状态，也不能未经授权直接移除限制。

### P1 · 余额展示和管理总览断层

关联余额在数据库里，但个人网页投影丢了 `carrot/carrot_at`；“签到萝卜”显示单次所得。管理员跨账号总览方法没有接入口，API 返回的 `carrot` 没有相应完整页面呈现。

### P2 · 白名单和资源边界需要明确

Bot 白名单管消息与回调，不管已建立的个人网页会话。空闲连接缓存最多 3，不是活跃连接最多 3；实跑同时借到 5 个，归还后只缓存 3 个。70 次不同 IP 的单次登录失败留下 70 条 `until=0` 行。

## 复验命令

本会话证据目录：`/var/minis/workspace/emoskata-takeover-20261108`。复验使用临时 SQLite、随机端口和假官方 profile，不接触生产账号。

```sh
python3 /var/minis/workspace/emoskata-takeover-20261108/verify_gaps.py
cd /var/minis/workspace/emos-sign
PYTHONPATH=.:../emoskata-takeover-20261108/tests \
  python3 -B -m unittest discover \
  -s ../emoskata-takeover-20261108/tests -p 'test_*.py' -v
python3 ../emoskata-takeover-20261108/probe8.py
python3 ../emoskata-takeover-20261108/probe6.py
```

探针断言刻画“当前缺口确实存在”，它们通过不是功能验收通过。源码变更后应更新这些判据，而不是为旧输出维持 bug。

最终源码位置以 `rg` 复验，不依赖重构前行号：

```sh
rg -n 'def login_with_token|def scope|def visible|def admin_overview|def set_input' accounts.py
rg -n 'set_input\(tg_id, .token|def _guide_token|def _add_by_token|def _show_login' bot.py
rg -n 'def state|def _basic|last_carrot' webui.py
rg -n 'def _probe_core|def _prepare_core|def _run_ids|def main|_DB_IDLE_LIMIT|until<>0' app.py
```

## 本轮注释重构

- 24 个 Python 源文件统一模块责任、函数契约、行内原因；重建 `emoskata.py`，不直接编辑内嵌串。
- Dockerfile、安装/容器准备脚本和 CI 注释同样统一；逐行对照确认去掉注释后的部署内容与基线一致。
- 删除“我以前踩过”“真 bug 真踩过”等散落复盘，保留非幂等、TG 归属、一跳授权、并发、文件描述符和迁移的必要解释。
- 把“启动自检”准确写成“导入预检”，把“连接池最多 3 个”改成空闲缓存限制，把“普通网页只读”改成可提交本人的任务。
- 不修改 user-facing 文案或业务控制流，避免把注释整理变成未经要求的权限/行为更改。
- 去文档串的源文件 AST 逐一对照基线；生成模板只改生成头文档串，内嵌模块行为 AST 也逐一核对。结构比较仅证明未改逻辑，行为仍以测试为准。
- 统一后续规则见 [MAINTENANCE.md](MAINTENANCE.md)。

## 本轮实际执行结果

| 验证 | 结果 |
|---|---|
| 源码行为 AST | 24 个文件与 50fe5dd 一致 |
| 生成入口 | 7 个内嵌模块及 bootstrap 去文档串 AST 一致 |
| 部署内容 | Dockerfile/compose/sh/CI/.gitignore 去注释后执行行一致 |
| 重构后 unittest | 231 项 OK，121.921 秒 |
| 兼容 selftest | 231 项 OK，84.273 秒；前次 120 秒超时不计通过 |
| Node 官方 JS 语法检查 | 四份真实渲染脚本通过 |
| pyflakes / shell 语法 / diff 空白 | 通过 |
| 单文件漂移检查 | 通过，270275 字节 |
| 额外域审缺口探针 | 主审独立复跑 16 项 OK；它们在确认缺口，不计作功能通过 |
| 无效绑定重启演练 | 获 202 后退出 1：缺口成立 |
| 不兼容 SQLite 路径重启演练 | 获 202 后退出 1：缺口成立 |

当前单文件 md5 `78d44e6d00c82c492268995de97e7af5`；sha256 `b4c2f7ac59e82a3c69aa143c7fedaf3aa7720f153ad46255f6ca5b3c854181b4`。完整脚本和原始日志保存在外部审计目录，未把领导聊天原文或真实密钥纳入仓库。

## 交付边界

本报告不宣称“代码写了就已经完成”，不把“231 项 OK”变成生产授权、全账号管理、完整重启或真实容器验收。缺口等待单独修复，官方实机链路等待 YG 的生产环境验证。
