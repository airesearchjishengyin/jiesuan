# 借算 Windows 节点 · 一键接入指南

目标: 让 Mac 上的 Hermes(我) 能 SSH 进你的 Windows 机器, 并把它配置成借算算力节点。
全程约 15 分钟, 90% 是复制粘贴。

---

## 第 0 步 · 网络通道 (二选一)

**A. 两台机器在同一个家庭/办公室局域网** → 什么都不用做, 直接用内网 IP。
   (Windows 上查 IP: `Win+R` → `cmd` → `ipconfig` → 看 "IPv4 地址", 如 192.168.0.x)

**B. 不在同一网络** → 两边都装 Tailscale (免费, 5 分钟):
   - Windows: https://tailscale.com/download/windows 下载安装, 登录同一账号
   - Mac: `brew install --cask tailscale` + 菜单栏登录同一账号
   - 之后用 Tailscale IP (100.x.x.x) 代替内网 IP

---

## 第 1 步 · Windows 开启 SSH 服务端 (管理员 PowerShell)

按 `Win+X` → 选 **"终端(管理员)"**, 逐段粘贴:

```powershell
# 安装并启动 OpenSSH Server
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic

# 放行防火墙 (SSH + Ollama + jiesuan-node)
New-NetFirewallRule -DisplayName "SSH" -Direction Inbound -Protocol TCP -LocalPort 22 -Action Allow
New-NetFirewallRule -DisplayName "Ollama" -Direction Inbound -Protocol TCP -LocalPort 11434 -Action Allow
New-NetFirewallRule -DisplayName "JiesuanNode" -Direction Inbound -Protocol TCP -LocalPort 7801 -Action Allow
```

## 第 2 步 · 免密登录 (把我 Mac 的公钥交给 Windows)

管理员 PowerShell 继续:

```powershell
# 创建 authorized_keys (把下面整行换成你 Mac 上显示的公钥内容)
$pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEuGiT0Dm8I/qP7FxO6IT4PgW6J2/fi4KDjUsBLso24p jiesuan-mac-to-win"
$authFile = "$env:ProgramData\ssh\administrators_authorized_keys"
Add-Content -Path $authFile -Value $pubkey
# 修权限 (Windows OpenSSH 对管理员账户的要求)
icacls $authFile /inheritance:r /grant "SYSTEM:F" /grant "BUILTIN\Administrators:F"
Restart-Service sshd
```

> ⚠️ 粘贴时保持整行完整。公钥也可以从 Mac 重新打印: `cat ~/.ssh/jiesuan_win.pub`

## 第 3 步 · 告诉我 IP

回来告诉我: **Windows 的 IP** (第 0 步查到的) 和 **登录用户名** (你的 Windows 账户名)。
我会先测试 SSH 连通性, 然后接管剩下的全部:

- 安装 Ollama for Windows + 拉取模型
- 安装 Python + jiesuan-node + 注册成 Windows 服务 (开机自启)
- 防火墙最终检查 + 双机联调 + 压测验收

---

## 你需要准备的唯一决定

**要不要在 Windows 上拉 qwen3:14b?**
你的 N 卡显存多大, 就能跑多大:
- 8GB 卡 → qwen3:8b
- 12-16GB → qwen3:14b (和 Mac 侧对齐, 推荐)
- 24GB → qwen3:32b / qwen2.5:32b (异构优势: 大模型只有它能跑)

回复时带上显存大小, 我直接安排。
