# 维护地图

此文只描述当前代码。注释格式见 [COMMENT_STYLE.md](COMMENT_STYLE.md)，本次亲自运行的验收见 [QUALITY_REVIEW.md](QUALITY_REVIEW.md)。历史报告不作为当前功能判据。

## 模块与边界

| 文件 | 职责 | 调用约束 |
|---|---|---|
| `envconfig.py` | `.env` schema、校验、锁、原子保存与缓存 | 初次播种环境值；运行以文件为准，重启字段固定到下次启动 |
| `emos.py` | JSON 请求、身份及签到结果 | 自定义域不追加公共域；请求撤销守卫由调用方注入 |
| `accounts.py` | 官方主号证明、账号、选择及域事务 | 证明保留不代表仍登录；事务内外复核当前 TG 配置 |
| `bot.py` | 私聊路由、菜单与有界任务 | 菜单不等于权限；offset 确认与业务事务不是恰好一次提交 |
| `webui.py` | 一次性入口、网页会话、账号视图和任务 | 不拥有监听；任务与退出确认存在内存，worker 按需运行 |
| `app.py` | SQLite、HTTP、调度、通知及生命周期 | 总物理连接八个、空闲缓存三个；事务内复用 conn；更新前排空 |
| `updates.py` | 固定源码、数据库快照与同 PID 恢复 | 配置导入前恢复；真正就绪后提交；没有常驻 watchdog |
| `runtime_info.py` | 按需进程统计和展示地址 | CPU 为两次采样间比例，Linux 内存不可读返回未知 |
| `build_single.py` | 八个隔离模块的单文件构建 | 保留旧版明文更新格式；维护模块修改后重建 |
| `check_web_js.py` | 渲染后四段脚本语法 | Node 是开发检查工具，不是运行依赖 |

## 配置与持久数据

- `EMOS_ENV_FILE` 选择 `.env`。相对路径以入口目录解析。
- `DATA_DIR` 在启动时确定；相对路径以入口目录解析，绝对路径原样使用。
- `.env` 保存系统配置。首次缺失才创建，已有值保留，仅补新增键；显式默认值和清空密钥优先于旧库迁移。
- SQLite 保存账号、密钥、主号证明、会话、设置与日志。缺库时建库，旧库按新版 schema 补表、列与索引，不重置登录数据。
- 单文件只从内嵌模块加载；上传新版并重启会更新模块、网页、脚本和模块注释，不需要也不会覆盖旁边的维护 `.py`。
- 成功源码和更新事务固定在 **配置目录**，不是一律在程序目录。默认名为 `.emoskata.py.last-good` 与 `.emoskata.py.update-state.json`。
- `carrot`/`carrot_at` 是缓存余额；`last_carrot` 是单次所得，不能互换。
- 退出清本地密钥、撤销待授权和旧任务，保留账号与日志；不使官方 Token 本身作废。

## 更新调用顺序

1. `_prepare_core` 固定候选字节，检查内嵌模块和隔离 `main()` 初始化；源码、配置、SQLite 使用副本。
2. `_drain_core` 拒收新任务，检查请求、线程、连接与通知是否归还。
3. `_stop_core` 停线程；`exec_update` 用 SQLite backup 保存包含 WAL 提交的数据快照。
4. 候选版初始化、绑定监听、创建核心线程后才 `mark_ready` 并释放业务门闩。
5. 就绪前可捕获的启动失败进入新的干净 Python，再恢复源码、配置和数据库。

已接受的源码通过 fd 传递，后续删除或重新上传不改变这次 exec 使用的字节。入口本身缺失时恢复可信快照并要求再次重启；缺依赖不得覆盖现有入口。恢复发布不覆盖期间刚上传的新文件。

## 不能混为一谈

- 创建线程与监听成功，不等于真实 Telegram 或 emos 请求成功。
- 稳态单进程，不等于预检期间没有临时子进程。
- 入口删除后旧进程能服务，不等于停掉进程后没有入口也能被 Python 启动。
- 当前版本拒绝坏候选，不等于任意上传代码是安全沙箱。
- 非幂等写请求的超时可能已有远端副作用，不能承诺恰好一次。
- 同 PID 更新保数据的前提是配置、数据路径与持久目录保留；不自动搬迁数据。

## 验收命令

```sh
python3 -B -W error::ResourceWarning -m unittest discover -s . -p 'test_*.py'
python3 -B check_web_js.py
python3 -B build_single.py --check
python3 -B -m pyflakes *.py
sh -n install.sh
sh -n prepare_docker.sh
git diff --check
```

纯注释阶段还比较去文档串 AST，包括动态引导源码；结构相同不代替行为测试。`zz_*` 是固定目录手工演练，不作为当前正式验收入口。
