# 2026-11-09 独立接手质量核查

## 后续修复：公网地址、CDN、托管清单与 TG 公开访问

在已发布的 `8b048c633af3a7fb1801794a8311922238184eb2` 基线上完成本轮本地修复，未替换真实服务、配置或账号数据；本轮是否已发布以 GitHub 提交记录为准。

- **公网 IP 与 CDN 独立**：正常网页服务就绪后探测本机或出口公网 IPv4 一次；填了 `PUBLIC_BASE` 或 `WEB_BASE_URL` 也不取消探测。管理 API 和页面同时显示服务器公网 IP、CDN 管理入口、容器内网诊断地址。`172.19.*` 不再被打印成管理访问入口。
- **不伪造可达性**：无 CDN 配置时用公网 IP 和端口生成链接；NAT 出口不保证入站可达，防火墙、宿主映射、CDN 回源端口仍需实际验证。Compose 默认公开映射 `8080:8080`。探测失败保持未知，不退回容器内网地址；组播、保留和私有 IP 全部拒绝。
- **托管清单在实际运行目录**：镜像、安装目标与 `runtime/` 都准备 `requirements.txt`；已有文件不覆盖。`--prepare-host` 只准备清单，正常启动也补缺失清单，均不生成 `.env`。平台启动前检查文件的首次提示，须通过上传清单或提前执行准备命令消除；没有需要自动下载的 Python 包。
- **TG ID 留空公开**：非空只允许指定的一个正整数 ID；非法策略拒绝。公开用户各自完成官方首登、添加本人持有 Token 的其他账号、管理自己的选择/偏好/会话/签到，通知发本人。不恢复历史跨 TG 共享；管理员面板仍需密码。
- **验证不靠文字**：新增真实 `-I -S -B` 单文件进程，配置 CDN 后仍收到一次本地模拟公网服务请求，API 返回公网 IP、CDN 地址和容器地址；去掉 CDN 后仅更换访问链接，不重新探测。两用户 Bot 登录、账户隔离、个人网页、通知、调度和策略切换回滚均实测。
- **回归隔离**：普通更新回归只禁用夹具中的外部 IP 服务，地址功能由真实 localhost 专项覆盖；预检夹具改用持有的随机端口，不争用默认 8080。正式运行不禁用公网探测。

最新双版本测试数、耗时和入口指纹见 [VALIDATION.md](VALIDATION.md)。下面的独立接手过程和资源数字属于上一轮证据，不冒充本轮的新资源测量。

## 判定

**能作为 POSIX 纯 Python 单文件服务运行；不是“只上传 py 不配任何文件”。** 部署需一个 `emoskata.py`、已填写的 `.env`、Python 3.10+ 标准库及可写持久目录。Node、pip 包和维护模块不是运行依赖。

本轮从 `main / 2cbb3b4de0df0d973585775cff72a04102a8fb1c` 实际源码接手，自己阅读八个运行模块、生成器、资源工具、部署/CI 与相关回归。没有委派验收，不以前任注释或历史报告作证。本报告记录源码与本地验收；发布和 CI 状态以 GitHub 实际提交、运行记录为准，未替换真实 Bot 或生产程序。

## 本人实际执行的单文件场景

独立探针只在隔离目录写合成配置和账号，使用 `python3 -I -S -B -u emoskata.py`。`-I -S` 禁用外部 Python 路径和 site-packages；runtime 内只有一个 py。

| 场景 | 实际输出与判据 |
|---|---|
| 启动 | `/api/health=200`、`/login=200`、无认证账号接口 `401`；runtime py 列表只有 `emoskata.py` |
| `.env` 不存在 | 退出码 2，提示 `.env.example`，不创建部署文件或数据库 |
| 只读配置校验 | exit 0，配置 SHA256 不变，不建数据目录或成功快照；不执行 app/updates |
| 网页保存 | 登录 → 用实际 revision 保存 `SIGN_TIME=09:45/GAP_SECONDS=11` → 文件哈希改变；重启读回相同值 |
| 删入口、服务仍在运行 | health 200；点重启得到 400，恢复可信成功快照；原配置和数据库未变 |
| 完整上传后更新 | 202 接受、最终运行标记 v2，PID 不变；全部表内容、schema、配置与个人会话保留 |
| 语法坏文件 | 400 拒绝；旧版继续服务，账号和日志无变化 |
| 未就绪启动失败 | 候选已真实改配置、改 Token、建表再抛错；日志确认修改发生；干净 exec 恢复旧镜像、原配置、全部逻辑数据和有效会话 |

数据夹具包括主号、额外号、另一 TG 的历史账号、保留关系、余额、日志、用户选择/偏好、管理员会话、个人会话与一次性网页入口。完整内容与 schema 分别比较，不只对账号条数或状态文案断言。

**回滚不要求数据库文件逐字节相同。** SQLite backup 会改变数据库 header、页布局或 WAL 状态；实测 md5/sha256 可改变，但所有表内容和 schema 的序列化摘要完全一致。这不等于丢数据。正常同 schema 更新的数据库与配置字节本轮一致。

## 本轮修复的真实缺陷

1. **`--check-config` 会创建数据目录。** 原引导导入 app 后才做校验。现在只加载 `envconfig`，不执行更新恢复和应用导入；缺失/合法配置都新增无副作用回归。
2. **读取/保存的密码判据不同。** 纯 Bot 读取允许空面板密码，update 的严格判据却拒绝改副本/保存。现在读写共用配置完成与校验逻辑；启用网页仍要求有效密码。
3. **显式保存缺失默认值只留内存标记。** 用户保存 `SIGN_TIME=08:00` 时 old 默认也是 08:00，未写盘；新进程认为未配置，旧库可覆盖。现在显式保存缺失键会写盘，来源随文件重新读取；重启后的默认选择保留。
4. **安装器不先校验 `.env`。** 加入无副作用校验再进入启动。安装回归 PATH 明确排除 systemctl，不能在 root CI 里意外改宿主服务。
5. **签到 blocked 仍会立即换域再发 PUT。** 真实 localhost 模拟先产生远端写入再返回 403/HTML，原实现会第二次提交。现在写方法 network/blocked 都停止即时降级重发；GET 备用域保留。新增两个回归覆盖真实副作用及 PUT/POST/DELETE。
6. **回归夹具泄漏 SQLite 连接。** 官方 Connection 上下文只提交/回滚，不自动 close。公共夹具自动归还池；Bot/更新测试用 closing 明确关闭短期连接，Python 3.14 全量输出不再含 ResourceWarning。
7. **三个手工演练不可用/假通过。** 硬编码旧路径、靠环境生成配置、模糊 PID、失败退出码 0，均换成正式回归入口。三个文件名保留；所有演练实跑成功。
8. **更新备份有凭据风险。** `.update-stage-*` 含原配置和数据库；已加入忽略规则，禁止随源码提交发布。

官方连接契约：[Python sqlite3 Connection context manager](https://docs.python.org/3/library/sqlite3.html#how-to-use-the-connection-context-manager)。本轮只修自身代码/环境，不写脚本模拟官方 lint 或 JavaScript 解析。

## 注释重构

- 统一模块职责、函数契约、事务边界、失败返回和理由注释；不保留作者故事或把旧报告复制成判据。
- 重写所有运行模块、开发工具与测试的模块说明，以及约 200 处关键契约；普通 getter 不凑注释覆盖率。
- 修正“纯配置校验不建文件”“join 等线程全退”“取消授权清输入”“兑换码立刻建立权限”“只有网页写任何配置”等与实现不一致的说明。
- 功能修复、工具替换和纯注释阶段分别保存基线。注释阶段 37/37 文件的去文档串 AST 一致，递归包含八个生成模块、bootstrap 和更新公共动态源码。
- 后续发现的写请求防重复修复、安装测试隔离和删除重复测试断言单独记为功能/测试修改，不计入“纯注释无逻辑变更”的承诺。
- 生成入口重新由维护模块构建，不手改 `emoskata.py`。网页模板、SQL 和业务文案不因注释整理改变。

## 资源：实测与边界

全部为 Linux/Android 沙箱中的 60 秒隔离样本；不代表生产远端、网络故障或满负载。

| 样本 | 单核 CPU | RSS 样本 | 峰值 / 线程 |
|---|---:|---|---|
| 未修改基线，面板空闲 | 0.050% | 21.64 / 21.64 / 21.64 MiB | 37.94 MiB / 4 |
| 最终入口，面板空闲 | 0.033% | 22.04 / 2.24 / 2.24 MiB | 38.02 MiB / 4 |
| 最终入口，本地真实 Bot 长轮询 | 0.050% | 23.48 / 9.14 / 9.24 MiB | 24.09 MiB / 5 |

RSS 降低伴随 Android 驻留页回收，**不能归因于代码优化**，更不能称运行内存只要 2 MiB。较稳妥的预算是本次冷启动/认证样本约 24–38 MiB 峰值；真实业务并发另测。

实现约束：一个应用进程；调度/Event 等待、通知阻塞等待、长轮询及退避；单网页 worker 按需启动；HTTP 请求有上限；SQLite 8 总物理连接、3 空闲缓存、每连接 256 KiB 页缓存；日志有保留上限。没有为了数字削弱 scrypt、取消权限复核或关闭完整更新回滚。

## 仍保留的边界

- 保数据前提：只换 py，保留原 `.env`、`EMOS_ENV_FILE`/`DATA_DIR`、数据库、WAL/SHM 和持久卷。变更路径不会自动搬库。
- 空窗期间旧进程能服务，不代表被停后没入口也能启动。先上传完整入口再重启。
- 初始化、监听、线程成功不等于官方授权/签到成功；不可捕获退出、断电、任意恶意候选不承诺即时回滚。
- 纯 Bot 没有网页核心重启入口，须通过宿主正常重启；更新持久数据语义相同。
- Telegram 客户端使用 urllib 默认重定向行为，本轮没有实现/声称专门的跨站重定向拒绝策略。
- Docker 镜像/systemd 生产启动、真实 Telegram/emos 授权、并发上限压测未执行。不拿静态部署语法检查冒充这些验收。

## 可复验入口

```sh
python3 -B -m unittest discover -v
python3 -B -m unittest test_takeover_contract test_config_upgrade test_client
python3 -B -m unittest test_update test_single_file_upgrade test_updates
python3 -B check_web_js.py
python3 -B build_single.py --check
pyflakes *.py
sh -n install.sh && sh -n prepare_docker.sh
git diff --check
python3 -B zz_restart_rehearsal.py
python3 -B zz_badpy_rehearsal.py
python3 -B zz_crash_rehearsal.py
```

本轮独立探针与原始结果保存在接手工作目录的 `verify_runtime.py`、`probe_contract.py`、`verify_bot_idle.py`、`release-runtime/runtime-results.json`、`release-bot-idle/bot-idle-results.json`、`release-contract/contract-results.json`、`comment-ast-verification.json`、`mutations-final/mutation-results.json`。新增四个缺陷恢复变异对应测试全部失败，证明这些特定判据有效，不代替整体行为验收。

当前回归的版本、耗时和指纹统一放在 [VALIDATION.md](VALIDATION.md)，避免多个文档重复维护不同数字。
