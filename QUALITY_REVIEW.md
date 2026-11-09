# 当前单文件与配置保留验收

本次从当前源码和本人执行结果判断，不沿用历史验收。验收仅使用本地源码、隔离测试目录，没有读取或修改真实账号凭据，也没有替换真实 Bot 运行文件。提交与发布状态以 Git 记录为准。

## 单文件更新的实际含义

| 对象 | 本次实际验证 |
|---|---|
| 运行代码 | 删除旧 `emoskata.py`、上传新版至原路径、重启；8 个内嵌模块全部加载新版 |
| 页面、脚本和模块注释 | 随单文件一并更新，前后两个版本的标记不同 |
| 旁边的维护 `.py` | 不读取、不提取、不覆盖；单文件不依赖它们 |
| 缺失 `.env` | 拒绝启动（退出码 2）并提示复制 `.env.example`；不创建配置，也不建数据库 |
| 缺失数据库 | 自动创建数据目录、数据库、表、索引与成功快照 |
| 已有 `.env` | 只读；缺键按默认值补进内存，文件字节不变；只有网页保存才写回 |
| 已有数据库 | 新增表、列、索引；账号、Token、设置、余额、日志和有效网页会话保留 |
| 就绪前启动失败 | 恢复旧源码、配置和数据；测试包含配置与数据库已被候选修改后抛错 |

验证使用 `python3 -I -S -u`、完整 localhost HTTP 服务和合成账号，不依赖外部源码或 pip。运行要求 Python 3.10+、标准库 SQLite，以及配置时区对应的系统时区数据。

## 本次修复

旧库迁移原先按“当前值等于默认值”推断字段未配置，会把 `.env` 中显式默认值和清空 Token 覆盖。现按实际提供的键判断，旧库只能补缺失项。程序不再播种进程环境、不再生成配置：`.env` 由部署者提供，缺文件即拒绝启动，缺键只在内存补默认值，非法写入不会写文件或改变来源记录。

配置回归验证默认签到时间、间隔、空 Token 和后续保存的默认值不被旧库覆盖。隔离副本恢复旧错误时，对应测试失败；实际工作树没有注入变异。

## 当前产品合同

一个配置的 TG 入口管理多个独立 emos 账号。首号官方授权，其他有效 Token 可属于不同 TG 或未绑定 TG。`/sign` 只签当前账号，`/signall` 全部；Bot 和网页均有两个按钮。Token 添加提示保持一句话，校验和操作白名单保留。

## 注释与维护

统一模块职责、函数契约、失败语义与行内原因；不以覆盖率作质量判据。纯注释阶段 35 个文件去文档串 AST 一致；动态启动源码单独编译，8 个内嵌模块逐项与维护源码一致。AST 相同不代替运行验证。

规范见 [COMMENT_STYLE.md](COMMENT_STYLE.md)，路径和时序见 [MAINTENANCE.md](MAINTENANCE.md)。

## 本次亲跑结果

- 325 项标准库回归通过，172.537 秒，启用 ResourceWarning 检查。
- 配置保留与跨版本升级的新增测试共 10 项；观察真实新旧代码、schema、会话和账号内容。
- pyflakes、四段真实 Node 脚本、shell 语法、生成漂移及 diff 空白检查通过。
- 子代理使用固定 DeepSeek 非当前模型；两路超时，无最终报告，不计验收。

## 本次资源采样

空库、无真实 Bot Token、不发外网请求。记录本人启动的 PID 22896，采样 60.04 秒。

- CPU：0.067% 单核。
- RSS：22.03 MiB；历史峰值：37.70 MiB。
- 4 线程、1 个应用进程，正常停止退出码 0。

不把空闲短样本当成生产上限，也不声称本轮已证明显著降幅。

## 数据保留的必要边界

只替换程序，不删除 `.env`、数据库、WAL/SHM 和持久卷，不改变 `EMOS_ENV_FILE` / `DATA_DIR` 的原路径。若删除数据库，创建的是空库，不是恢复旧登录。会话仍受有效期、主动退出和官方 Token 撤销约束。代码上传后须重启才激活；纯 Bot 模式没有等价的在线重启按钮。

任意上传代码不是安全沙箱；不可捕获的进程退出不能承诺立即回滚。新数据格式是否兼容仍由新版迁移代码决定。本次没有 Docker 镜像实建、真实官方授权或满负载资源验收。

## 复跑与原始结果

```sh
python3 -B -W error::ResourceWarning -m unittest discover -s . -p 'test_*.py'
python3 -B -m unittest test_config_upgrade test_single_file_upgrade
python3 -B check_web_js.py
python3 -B build_single.py --check
python3 -B -m pyflakes *.py
sh -n install.sh && sh -n prepare_docker.sh
git diff --check
```

原始输出：`/var/minis/workspace/emoskata-takeover-20261109-v3/` 的 `tests-final.log`、`creation-upgrade-tests.log`、`upgrade-final-focused.log`、`source-verification.json`、`resource-current.json`、`config-regression-proof.json`。

## 当前单文件指纹

- 字节：346257
- md5：`2e0fffa4fa7d321e08c2bc83107cc580`
- sha256：`d51942e3b7598eea989d6dfaacee3325d697bf98dd0490caf0d0db76d2e5f531`
