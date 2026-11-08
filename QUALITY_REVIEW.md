# 当前工作树亲审结果

本次结论来自读取当前源码和本人执行的测试、隔离运行与采样；不引用旧记忆、旧验收或子代理结论。验收只涉及本地代码，提交与发布状态以 Git 记录为准；没有替换正在运行的 Bot。

## 已验证的运行合同

| 要求 | 本次实际结果 |
|---|---|
| 只运行一个 Python 文件 | 独立目录只放 `emoskata.py` 和测试 `.env`，`python3 -I -S -u` 启动；不依赖源码目录、site 或 pip |
| 稳态一个进程 | 自己启动并记录的 PID，实际父子进程关系核对为仅该进程；四线程 |
| 删除 py 再上传 | 同目录重新上传后真实 `/api/restart`；候选已加载，PID 不变 |
| 数据保留 | `.env` 原字节一致；十二张 SQLite 表全部逐行一致，包含主号、额外号、余额、设置、登录会话和日志 |
| 外置配置 | `.env` 与入口分目录重复同样验收，缺失入口也能找到成功快照 |
| 真启动失败回滚 | 候选通过预检后修改 `.env`、数据库再抛错；旧源码、配置、所有数据恢复，同 PID |
| 坏上传拒绝 | 语法错误返回 400，旧服务仍健康，不切换 |
| 结束测试 | 所有自己启动的验收进程都按记录 PID 终止，退出码 0 |

这里是本地测试账号与数据库，不是对真实 emos/Telegram 账号授权或签到的验收。

## 本次修复

1. **缺失入口的快照位置不一致**：成功快照实际写在配置目录，恢复原先去入口目录找。现统一由 `updates.restore_missing_entry` 处理，外置 `.env` 生效。
2. **误恢复范围**：缺少分模块依赖的 FileNotFoundError 不能被解释为入口删除。只对入口本身缺失走恢复，并以排他发布避免覆盖正在上传的新文件。
3. **证明与登录混淆**：主号的历史官方证明仍存在，不等于主号仍有有效本地密钥。调度、通知及请求前判据使用有效登录；空白 Token 不再判为可执行。
4. **同 Token 重授权的旧结果**：账号快照携带登录代际，写回事务复核 owner 与代际，旧请求不能把重新授权的账号停用或写旧日志。
5. **无谓开销**：通知线程阻塞等待而非每秒空转；信号处理只设 Event，普通收尾才唤醒队列；重复收尾不累积哨兵。同内容成功快照不反复原子重写；模块导入也清源码字典和末尾引用。
6. **备份边界**：预检和更新统一使用 SQLite backup，包含已提交 WAL；设置进度超时并关闭所有已创建的连接。

没有引入压缩源码协议：旧运行版本的预检只认识明文 `_SOURCES`。为了节省少量内存而破坏“只上传 py 更新”的合同不合算。

## 注释与维护

模块职责、函数合同、行内原因统一；修正了实际没有做到的“取消必增代际”“can_run 根与目标都启用”“网页 stop 关闭监听”“offset 与业务恰好一次”等说明。

维护及测试文件的纯注释阶段比较去文档串 AST；最终八个运行模块与生成器再次逐一复核，动态引导源码也解析比较。内嵌运行源码与构建器的真实输出一致。AST 一致只证明注释阶段未夹带执行结构变化，不代替行为测试。

动态引导与生成模板保持多行可读。规范见 [COMMENT_STYLE.md](COMMENT_STYLE.md)，生命周期地图见 [MAINTENANCE.md](MAINTENANCE.md)。

## 亲测资源

条件：空库、无 Bot Token、不发外网请求，单次采样约 35.03 秒。

- CPU：0.0285% 单核；本次窗口仅 1 个 CPU tick，精度不足以证明显著降幅。
- 稳态 RSS：21.94 MiB；进程历史峰值：37.69 MiB。
- 线程：4；fd：5；进程树：[19351]。
- 与本次输入入口对照：内存没有显著下降。不把来源释放或短 CPU 样本夸成大幅优化。

## 当前验收

- 305 项 unittest 通过（124.535 秒），启用 `-W error::ResourceWarning`。
- pyflakes、四段真实 Node 语法、两份 `sh -n`、`git diff --check`、`build_single.py --check` 全通过。
- 三处修复分别禁用的隔离变异都令对应回归失败；未改工作树做变异。

本地可复跑：

```sh
cd /var/minis/workspace/emos-sign
python3 -B -W error::ResourceWarning -m unittest discover -s . -p 'test_*.py'
python3 -B check_web_js.py
python3 -B build_single.py --check
python3 -B -m pyflakes *.py
sh -n install.sh && sh -n prepare_docker.sh
git diff --check
python3 -B /var/minis/workspace/emoskata-own-review/check_runtime.py
python3 -B /var/minis/workspace/emoskata-own-review/check_resources.py
python3 -B /var/minis/workspace/emoskata-own-review/check_mutations.py
```

原始产物在 `/var/minis/workspace/emoskata-own-review/`：`tests-final.log`、`runtime-results.json`、`resources.json`、`mutations.json`、`final-source-verification.json`。它们是本人本次命令的输出，不是前任日志。

## 必须保留的边界

- 保留 `.env`、数据目录、持久卷及原路径；改变路径不自动搬迁数据。
- 如果 py 已删除且进程被终止，Python 没有入口可启动；需先补入口。运行中的恢复与 pending 恢复不能代替外部启动器。
- 就绪代表配置/数据库初始化、监听和线程创建完成，不代表真实官方网络业务成功。
- `os._exit` 或杀进程不保证即时回滚；下次能启动入口时处理 pending。
- 无 Docker 实际构建验收，无真实 Token 或满并发资源上限验收，不承诺任意上传代码是沙箱。

## 交付指纹

`emoskata.py`：343264 字节。

- md5：`c9a25792c0b686459dcfa574f74cb201`
- sha256：`0cf7f6b4995902e18cfe7f00e45ed9e6a767671f8e8096379205cc3b58dfeb5a`
