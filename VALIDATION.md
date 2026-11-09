# 当前验证记录（2026-11-09 地址与公开访问修复）

基线：`main / 8b048c633af3a7fb1801794a8311922238184eb2`。本轮改动本地完成，未替换真实 Bot、生产配置或账号数据。发布与 CI 状态以 GitHub 实际提交和运行记录为准。

## 最新实际结果

- Python 3.12.15：**361 项通过，203.678 秒**，`release-all-312.log`。
- Python 3.14.10：**361 项通过，201.721 秒**，`release-all-314.log`；无 ResourceWarning。
- 四段真实 JavaScript 的 Node 解析、pyflakes、两个 sh 语法、生成漂移和 `git diff --check` 通过。
- 配置了 CDN 的真实单文件进程（`-I -S -B`）仍探测公网 IP；一次本地 HTTP 探测后 API 同时返回源站 `8.8.8.8`、CDN 入口及容器 `172.19.0.203`。再次取面板状态不重探；移除 CDN 配置后访问链接改为公网 IP:端口，不改变探测缓存。
- 公网探测失败、私有/组播/保留/空/HTML/过大响应不冒充公网 IP；非关键探测线程创建失败不停止核心服务。
- `--prepare-host` 无 `.env`、禁第三方包可运行，只准备入口和工作目录清单；旧 requirements 保留。`--check-config` 不建清单/目录/库/快照、不发网络请求。
- 安装目标、Docker 镜像和挂载 `runtime/` 有 requirements；已有清单不覆盖。Compose 发布宿主 8080 以支持 IP 直连。
- TG ID 字符串留空公开，填一个合法 ID 限制该人；非法值不公开。两个用户的官方首登、Token、账户列表/操作、会话、通知与调度独立；全局 EMOS_USER_ID 不作为公开用户的默认账号。策略变化使事务回滚，指定模式撤销另一人的个人会话权限，但不删数据。
- 删除重传、同 PID 激活、坏语法拒绝、未就绪失败恢复、配置网页保存与原数据保留回归全部通过。

## 当前部署入口指纹

- 文件：`emoskata.py`
- 字节：365193
- md5：`c69c0037d4d2812d6a7e25f7bcd952a9`
- sha256：`7a82a823820b8269c2f10fec0b3eb7b998ffd5c59fa1edfaae89343026224a63`

## 复验命令

```sh
python3 -B -m unittest discover -v
python3 -B -m unittest test_hosting_address test_public_access test_owner_bot test_installer
python3 -B -m unittest test_update test_single_file_upgrade test_updates
python3 -B -m unittest test_takeover_contract test_config_upgrade test_client
python3 -B build_single.py --check
python3 -B check_web_js.py
pyflakes *.py
sh -n install.sh && sh -n prepare_docker.sh
git diff --check
```

## 范围

本轮证据：`/var/minis/workspace/emoskata-address-hosting-20261109/`。上一轮独立接手资源样本保留在 `emoskata-takeover-20261109/`，不能当成本轮新性能测量。公网探测是一次性后台任务，成功/失败缓存；没有增加固定周期公网轮询。

没有验收真实服务器端口/CDN 回源、防火墙、Docker 或 systemd 生产运行、真实 Telegram/emos 授权及生产并发负载。出口公网 IP 不构成入站可达性证明。真实配置不进仓库，完整模板与清单独立交付，程序仍不创建 `.env`，网页保存继续写回它。
