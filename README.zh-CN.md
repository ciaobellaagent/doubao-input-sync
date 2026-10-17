# Doubao Input Sync

把手机作为输入区，保留文字留档，并可选择粘贴到电脑当前光标所在的输入框。

[English](README.md) · [安全边界](SECURITY.md) · [运行与部署](docs/OPERATIONS.md)

这是非官方工具，与豆包或 WorkBuddy 无隶属关系。公开仓库为 [ciaobellaagent/doubao-input-sync](https://github.com/ciaobellaagent/doubao-input-sync)。本次开源采用源码快照，不包含私人部署历史、运行数据或操作记录；[LICENSE](LICENSE) 保留原 MIT 许可与署名。

## 三种模式

| 手机模式 | 文字如何到电脑 | 电脑端动作 |
| --- | --- | --- |
| 剪贴板（默认） | 手机复制，由输入法跨设备同步；同时把相同正文单独保存到 relay 留档 | 仅复制，或明确请求一次剪贴板匹配后的 Mac 粘贴 |
| 换气 | 草稿同步到 relay，点击发送后捕捉最终文字 | helper 复制或粘贴这批文字 |
| 顺口 | 草稿同步到 relay，停止输入一段时间后捕捉，默认 2 秒 | helper 复制或粘贴这批文字 |

电脑页面可以查看发送历史和持久保存的复制留档。服务端只使用 Python 标准库，网页无需构建。

**部署边界：这是个人单用户服务，没有内建用户认证。**房间号和设备占位不构成访问控制。默认只监听本机回环地址；手机接入前，应配置有认证的 HTTPS 网关，或只允许本人设备访问的私有 VPN。随机房间号不能保护暴露到公网的服务。详见 [SECURITY.md](SECURITY.md)。

## 本机启动

需要 Python 3.10 或以上。Node.js 只用于前端测试；可选的 Mac 原生辅助程序还需要 Xcode Command Line Tools 与 macOS 辅助功能权限。

```sh
git clone https://github.com/ciaobellaagent/doubao-input-sync.git
cd doubao-input-sync
python3 app/server.py --default-room demo
```

在这台电脑打开：

- 手机界面预览：`http://127.0.0.1:18766/mobile/demo`
- 电脑历史页：`http://127.0.0.1:18766/pc/demo`

`demo` 只是示例房间，不是密码。手机无法访问这台电脑的回环地址。手机实际使用需要自行配置受保护的 HTTPS 入口，并通过 `--public-origin` 指定准确来源；非回环监听还必须显式指定 `--allow-remote`。部署方法见 [Operations](docs/OPERATIONS.md)。

## 在电脑找回留档

手机和电脑使用同一个房间，例如 `/mobile/demo` 与 `/pc/demo`。手机点击复制后，已成功保存的文字会出现在电脑页的“复制留档”列表中，默认展示最近 50 条；更早的记录仍保存在数据库里。

电脑页打开时就读取留档，不需要启动自动粘贴接收器。页面可见时每 15 秒自动刷新，回到页面时也会重新读取；需要立即更新时，点击“刷新留档”。读取失败会提示重试，并保留已经显示的记录。

剪贴板模式下，PC 主框显示最新复制留档。换气／顺口模式尚无发送记录时也会先显示复制留档；有发送记录后，主框显示最近发送的文字，下方复制留档列表仍可查看。页面标明当前房间与记录时间，方便确认是否和手机配对。

点击记录上的“重新复制”可把文字放进这台电脑的剪贴板，随后在需要的位置手动粘贴。这一步不会再次留档，也不会发起新的自动粘贴请求。详见 [剪贴板与留档](docs/CLIPBOARD_ARCHIVE.md#desktop-history)。

## 手机复制 → Mac 自动粘贴

1. 配好手机的受保护访问入口，实际确认网页按钮复制能触发输入法跨设备剪贴板同步。
2. 编译安装本机辅助程序：

   ```sh
   python3 scripts/install_workbuddy_native.py
   ```

3. 从已具备 macOS 所需权限的终端宿主启动接收器。本机 relay 示例：

   ```sh
   python3 scripts/mac_prearmed_receiver.py --server-url http://127.0.0.1:18766 --room demo
   ```

4. 点中电脑上的普通输入框，在手机剪贴板模式点“复制并粘贴”。

按钮会立即复制，并在当前页面内等待电脑准备，最多 15 秒。编辑草稿或离开页面会取消尚未发出的动作。接收器核对 SHA-256 指纹、剪贴板、焦点和有效期，每次动作最多触发一次粘贴。仅准备不会粘贴，也不会自动拉取服务端正文兜底。

收到执行成功回执后，只清空本次未被改动的草稿，同时保留本地恢复入口；失败或结果不明时保留草稿。**回执强度有差异：原生路径会读回插入文字，普通 System Events 路径只能确认快捷键已发出。**如果电脑已有文字而手机显示失败，重试前先检查目标输入框，避免贴两遍。

普通应用通过系统焦点和标准 Command-V 粘贴，无需逐个配置应用名或网站。原生兜底路径先读取标准应用角色，再查找实际聚焦的输入框，支持按需准备辅助功能信息的应用。原生发键以不带修饰键的 V 松开事件结束；三个按键检查阶段同时核对应用层、会话和硬件状态，避免把尚未释放的修饰键带入下一次粘贴。不同应用仍需实际验收，不能据此保证所有编辑器都兼容。

**WorkBuddy 专项兼容：**WorkBuddy 使用辅助功能接口调用原生“编辑 → 粘贴”菜单（`paste:`）；其他支持的应用使用模拟 Command-V。这是明确保留的应用分支，不代表已证明 WorkBuddy 无法响应所有模拟快捷键。详见 [Mac 接收器](docs/PREARMED_RECEIVER.md)。

## 其他电脑端方式

上述 Mac 接收器也处理 relay 发送，请勿同时运行第二个粘贴 helper。仓库保留传统 Mac relay helper 和 Windows 复制／粘贴 helper；Windows 尚未实现 Mac 的提前准备与剪贴板匹配流程。先用仅复制模式，再逐个验证目标应用。参见 [Windows helper](docs/WINDOWS_HELPER_PLAN.md) 和 [运行说明](docs/OPERATIONS.md)。

## 数据与限制

- 复制留档把完整正文存入 Repo 外的 SQLite 数据库，重启后仍保留；列表只显示最近 50 条，不表示更早数据已删除。
- 剪贴板草稿、待补存记录和恢复文字保存在浏览器中；relay 草稿和发送历史保存在服务进程内存中。
- 手机复制成功、留档成功都不等于电脑已插入文字。
- Mac 自动化依赖系统权限、焦点和目标应用的辅助功能实现。带保护检查的剪贴板匹配路径拒绝密码输入框；不能把这个保证扩展到所有传统 helper。
- 手机、浏览器、输入法和桌面应用组合需要真机验收，单元测试不能证明普遍兼容。

清空、恢复与存储细节见 [剪贴板与留档](docs/CLIPBOARD_ARCHIVE.md)。

## 开发检查

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/test_client_clipboard.js tests/test_signal_trial_client.js
node --check app/static/client.js
```

集成测试请使用临时房间和数据库。发布前执行 [开源检查清单](docs/RELEASE_CHECKLIST.md)；贡献问题记录时请使用虚构文字，不上传实际剪贴板内容、数据库或凭据。

## 许可

[MIT](LICENSE)。
