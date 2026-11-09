# 接手后的维护地图

只描述当前源码的职责与边界；质量结论见 [QUALITY_REVIEW.md](QUALITY_REVIEW.md)，注释方法见 [COMMENT_STYLE.md](COMMENT_STYLE.md)。旧报告不能替代源码和行为。

## 部署与维护分开

**部署运行：Python 3.10+、一个 `emoskata.py`、部署者提供的 `.env`。** 不需要旁边的维护 `.py`、pip 包或 Node。`requirements.txt` 只含注释，没有依赖项。运行要求 POSIX 文件锁和 Python 标准库 SQLite；默认时区使用系统时区数据库或有限固定偏移回退。

- `python3 emoskata.py`：完整初始化、监听与业务线程，创建缺失数据库。
- `python3 emoskata.py --prepare-host`：只补入口目录与当前工作目录的 `requirements.txt`，不需要 `.env`、不建库、不发请求。托管器先于 py 检查文件时，首次上传仍需先准备此清单。
- `python3 emoskata.py --check-config`：只执行配置模块，不导入 app/updates，不建目录、数据库、成功快照或清单，也不恢复 pending。
- `python3 build_single.py`：开发时生成部署入口；`--check` 检查维护模块是否与入口一致。
- `check_web_js.py` / pyflakes / unittest：开发验收工具，不是运行依赖。

## 模块责任

| 文件 | 负责什么 | 必须守住的边界 |
|---|---|---|
| `envconfig.py` | Schema、文件解析、来源指纹、原子保存 | 不创建部署 `.env`；缺键只补内存；读写共用判据；网页显式保存缺失默认键会落盘 |
| `accounts.py` | 账号身份、主号证明、退出、设置和历史关系 | DB 前后同时核用户与策略；公开也只管理本人账号；主号证明不等于有效 Token；历史共享不开放 |
| `emos.py` | HTTP、账号资料与单次签到归一 | 请求前守卫；只有 GET 在网络/网关失败时换域；写请求不即时重发；未知余额不是 0 |
| `bot.py` | 私聊、菜单、输入代际、长轮询与有界队列 | 菜单不是权限；动作到达时冻结根和代际；回复失败不重做账号业务 |
| `webui.py` | 一次性入口、网页会话、账号视图、签到待办 | 不拥有监听；Cookie+CSRF+owner 核权；worker 空队列退出；任务结果不是持久日志 |
| `app.py` | SQLite 池、管理员 HTTP、调度、通知和生命周期 | 写事务复用 conn；8 总连接/3 空闲；有限 HTTP 线程；同 PID 更新前排空 |
| `updates.py` | 源码快照、pending、SQLite backup 与干净 exec 回滚 | 配置和连接之前恢复；真正就绪才提交；副本私有且路径校验；不是常驻 watchdog |
| `runtime_info.py` | 资源统计、独立公网 IP 缓存、托管清单准备 | CDN 不取消公网探测；就绪后一次性 worker 随即退出；失败不伪造内网为公网；清单不覆盖、不生成 .env |

## 数据位置和写入来源

- `.env` 路径由 `EMOS_ENV_FILE` 选择，**相对路径以入口目录为基准，不是 shell 当前目录**。
- `DATA_DIR` 启动时决定，相对值也以入口目录为基准。改路径会切换数据库，不会搬旧数据。
- 读取 `.env` 不创建、不补写。配置更新统一经 `ConfigStore.update()`：文件锁 → 按当前字节合并 → 校验 → 临时文件刷盘 → 原子替换。
- 写入来源包括管理员网页保存、一次性旧 SQLite 系统设置迁移；预检只会写配置副本。不能把“统一写入入口”说成“仅网页会写任何配置”。
- 密码空白保存保留现值；清 Token 必须明确清除。网页开启时密码必填；纯 Bot 允许空面板密码，须有效 Token。TG ID 字符串留空为公开，填一个正整数为指定用户，非法/缺失策略拒绝；公开不会开启历史跨 TG 共享。
- SQLite 保存账号、Token、余额、主号证明、网页会话、通知偏好、日志及保留的历史关系；缺库会建空库，**不是恢复被删的数据**。
- 成功快照 `.emoskata.py.last-good` 与 pending `.emoskata.py.update-state.json` 在配置目录。阶段目录可能包含原配置和账号库，已加入忽略规则，不得发布。

## 更新时序

1. `_prepare_core()` 固定完整候选镜像，校验外层和内嵌代码。
2. `_probe_core()` 在配置/库副本调用候选 `main()`；同监听端口使用临时端口，业务停在门闩。
3. `main()` 拒收新业务，反复检查待办、网络请求、HTTP 请求、通知和连接是否收尾。
4. `_stop_core()` 等线程退出并关闭池；失败取消更新，恢复旧核心。
5. `exec_update()` 备份原配置和包含 WAL 的数据库，在原 PID 进入候选。
6. 候选初始化、绑定与线程成功后 `mark_ready()` 提交，再放行业务。
7. 可捕获的未就绪失败进入干净解释器，恢复旧镜像、配置与库。

**入口缺失**：在线旧核心可恢复可信成功快照，返回 400 提示再次重启。不覆盖尚在上传的新文件；语法坏文件不自动覆盖。停进程时若入口还没补齐，Python 本身无法启动它。

**数据库恢复**：backup 保证逻辑内容；恢复后文件页布局、header 或 WAL 状态可以改变，md5/sha256 不同不等于数据丢失。内容与 schema 必须单独核对。

## 资源约束

- 公网 IP 与 CDN 是两条独立链：正常就绪后探测本机/出口 IP 一次，访问入口用 `PUBLIC_BASE`，个人链接用 `WEB_BASE_URL`。域名填了也探测；不解析 CDN DNS 冒充源站 IP、不把内网地址作为公网回退、不用缓存 IP 绕过 Host/CSRF 校验。
- 正常启动补缺失 `requirements.txt`；安装器、镜像与挂载目录预先携带清单。预检不准备文件、不启动公网探测；平台启动前提示不能由尚未运行的 py 消除。
- 稳态一个 Python 应用进程；预检期间才有临时子进程。
- 调度读本地数据并使用 Event 等待；通知阻塞等待并由停机唤醒，不忙轮询。
- Bot 长轮询；快速空响应和失败有退避。队列容量同时计算待办与运行中。
- 网页签到单 worker 按需启动；HTTP 请求上限来自配置，满额 503，空连接有读超时。
- SQLite 总物理连接固定上限，缓存每连接约 256 KiB；每账号日志有保留上限。
- 不靠削弱 scrypt、取消权限复核或缩短可靠收尾来制造“低资源”数字。

## 验收命令

```sh
python3 -B -m unittest discover -v
python3 -B check_web_js.py
python3 -B build_single.py --check
pyflakes *.py
sh -n install.sh && sh -n prepare_docker.sh
git diff --check
python3 -B zz_restart_rehearsal.py
python3 -B zz_badpy_rehearsal.py
python3 -B zz_crash_rehearsal.py
```

`zz_*` 保留原入口名，现调用正式集成回归，使用动态临时目录和准确子进程句柄；不另写判据，不再硬编码本机工作区。Python 3.14 回归另看真实日志是否有 ResourceWarning；仅把 warning 升级为 error 仍可能留下析构时的 unraisable 告警。
