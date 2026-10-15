# Use_Paramiko_To_Start_SSH_Tunnel

用 paramiko 在本地开 SSH 隧道：把远端才能访问的端口（内网 SOCKS 代理、只监听回环的
WebDAV 服务……）映射到本机 `127.0.0.1`。支持多跳同时跑、断线自动重连。

## 快速开始

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

cp conf_example.py conf.py
$EDITOR conf.py             # 填主机、用户、私钥，以及要转发的端口
python3 start_service.py    # 前台运行，Ctrl+C 干净退出
```

## 配置

`conf.py` 里放一个 `hosts` 列表（照着 `conf_example.py` 抄），每项的键：

| 键 | 说明 |
| --- | --- |
| `hostname` / `sshd_port` | SSH 服务器（默认 22，IPv6 直接写地址） |
| `username` | 登录用户 |
| `pkey` / `key_filename` / `password` | 认证方式三选一，按此顺序优先 |
| `remote_port` | 远端要连的端口 |
| `local_port` | 在本机 `127.0.0.1` 上开放的端口 |
| `target_host` | 远端从哪连过去，默认 `127.0.0.1` |

> `conf.py` 已被 `.gitignore` 排除：它写着你的主机名和私钥路径。

## 作为 systemd 服务

`ssh-tunnel.service` 是可直接改造的模板，**注意里面的 `ExecStart` /
`WorkingDirectory` / 日志路径要改成你自己的**（日志目录需要先存在，`file:` 不会创建它）：

```bash
sudo cp ssh-tunnel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ssh-tunnel.service
```

INFO 走 stdout、告警和错误走 stderr，日志因此分成两份，便于"平时不看、出事才看"。

## 断线处理

- SSH 断开会**指数退避重连**：1s → 2s → 4s … 封顶 60s；连接稳定存活 ≥60s 则从 1s
  重新开始（连上就断的抖动不会退化成每秒重连）；
- **空闲时每 5s 探测一次 transport**，静默断线（NAT 回收、对端重启）也能发现；
- SSH keepalive 30s；
- 收到 `SIGTERM`/`SIGINT` 立即停止，`systemctl restart` 不会留下残留 socket；
- 每个连接两条转发线程，任一端断开即同时关闭两端，不会泄漏半开连接。

## 自测

不需要 SSH 服务器和私钥——用假连接验证退避阶梯、失效探测、停止响应与数据转发：

```bash
python3 selftest_v2.py
```

## License

见 `LICENSE`。
