# 当前验证记录（2026-11-09 独立接手）

基线：`main / 2cbb3b4de0df0d973585775cff72a04102a8fb1c`。本轮为该基线上的独立接手改造，未操作真实 Bot、真实配置或账号数据。结论来自本人源码审查和隔离执行，详细边界见 [QUALITY_REVIEW.md](QUALITY_REVIEW.md)；GitHub 发布和 CI 状态以仓库实际提交、运行记录为准。

## 实际结果

- Python 3.12.15：最终 **346 项通过，174.729 秒**，日志 `last-tests-312.log`。
- Python 3.14.10：完整 **346 项通过，173.595 秒**，日志 `release-tests-314.log`；之后仅模板说明文字变更，最终 56 项重点回归又通过（26.097 秒，`last-focused-314.log`）。完整日志无 ResourceWarning。
- 一个 `emoskata.py` + 手写 `.env`，`python -I -S -B -u` 启动成功，health/login 200，无认证账号接口 401；runtime 无其他 py。
- 网页保存真实写 `.env`，重启读回；读配置不创建/补写；缺配置 exit 2；只读校验不建目录/库/快照，不执行应用或更新恢复。
- 删除入口、重传、同 PID 更新、错误语法拒绝和未就绪全启动回滚均通过；主号、额外号、历史账号、余额、偏好、会话、关系和日志保留。
- 原始单文件独立验收：`delivery-runtime/runtime-results.json`，入口 354598 B，pass=true，exit=0。另有 60 秒资源样本 `release-runtime/runtime-results.json`。
- 注释阶段 37/37 文件和递归内嵌源码 AST 一致；后续网络防重发/测试隔离/模板文案变更单独列为非注释修改。
- 四个新增配置缺陷变异对应测试全部失败；网络新增测试在修复前 6 个子断言失败，修复后通过。
- pyflakes、四段最终真实 JavaScript 的 Node 解析、两个 sh 语法、生成入口漂移和 `git diff --check` 通过。
- 三个 `zz_*` 入口分别调用 2/2/3 个正式回归，实跑全部成功。

## 资源样本

面板空闲与本地实际 Bot 长轮询各采 60 秒，CPU 约单核 0.033%–0.05%，4/5 线程；认证后冷 RSS 约 22 MiB，Bot 初始约 23.5 MiB，峰值约 24–38 MiB。Android 期间回收驻留页，后续低 RSS 不可归因为优化。不是生产或满负载上限。

## 当前部署入口指纹

- 文件：`emoskata.py`
- 字节：354598
- md5：`52470ae43ed09a55a09ed6b2d1e994c9`
- sha256：`f37dd119d2a5a61db12b9706487edae3d47e3a6de24373a0bcbf3aa386753be5`

## 复验

```sh
python3 -B -m unittest discover -v
python3 -B -m unittest test_takeover_contract test_config_upgrade test_client
python3 -B -m unittest test_update test_single_file_upgrade test_updates
python3 -B check_web_js.py
python3 -B build_single.py --check
pyflakes *.py
sh -n install.sh && sh -n prepare_docker.sh
git diff --check
```

原始证据工作目录：`/var/minis/workspace/emoskata-takeover-20261109/`。Docker 镜像、systemd 实际服务、真实官方授权/签到和生产并发负载未验收。本文件记录本地源码验收；发布后的 CI 结论以 [GitHub Actions 实际运行记录](https://github.com/dhrw444/emoskata/actions) 为准。
