# 安卓一体机现场探测工具

这个工具用于医院现场调研安卓医疗一体机，目标是判断后续能不能做到：

- 不安装客户端
- 不 root
- 尽量不改安卓系统
- 由 RK3588 自动截图、识别、输入、点击、取报告

脚本位置：

```bash
/opt/rk3588_gateway/scripts/android_site_probe.py
```

每次运行都会生成独立结果目录：

```bash
/var/lib/rk3588-gateway/android_site_probe/YYYYMMDD_HHMMSS_xxx
```

最新一次结果：

```bash
/var/lib/rk3588-gateway/android_site_probe/latest
```

主要结果文件：

- `report.txt`：现场可读结论
- `report.json`：结构化数据
- `FIELD_PLAN.txt`：现场插线和命令说明
- `commands/`：每条命令的 stdout/stderr 原始输出
- `adb_*.png`：ADB 截图
- `adb_*_window.xml`：ADB UI 树
- `hdmi_*.jpg`：HDMI RX 截图

## 最推荐：引导模式

到医院现场，如果不知道先测什么，直接运行：

```bash
sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode guided --label hospital
```

它会按步骤提示你：

1. 插 USB ADB 线
2. 测 Android 系统、截图、UI XML、输入法、存储、应用包
3. 插 HDMI 线
4. 测 HDMI RX 是否能看到安卓界面
5. 插 HID 线
6. 测 RK3588 虚拟键盘鼠标是否被安卓识别

如果你不想每一步按回车：

```bash
sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode guided --no-prompt --label hospital
```

## 只打印现场计划

```bash
python3 /opt/rk3588_gateway/scripts/android_site_probe.py --print-plan
```

## 路线 A：USB ADB

插线：

- 安卓一体机的调试口、device 口、OTG device 口
- 接 RK3588 的 USB Host 口

安卓端要做：

- 打开开发者选项
- 打开 USB 调试
- 屏幕弹出 RSA 授权时点允许

运行：

```bash
sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode usb-adb --label usb_adb
```

它会收集：

- Android 版本、SDK、品牌、型号、CPU ABI
- USB 状态：`sys.usb.config`、`sys.usb.state`
- 分辨率、DPI
- 当前前台应用包名
- 输入法状态
- UI XML
- ADB 截图
- 应用包列表
- Android features
- 系统 settings
- `/sdcard`、`Download`、`Documents`、`Pictures` 等目录
- 报告、打印、导出、PDF 相关文件或目录线索

判断：

- `USB ADB: usable`：这条路线最优，可以继续做 ADB 自动化
- `unauthorized`：安卓屏幕上没点 USB 调试授权
- 没有设备：大概率插到了安卓 Host-only 口，这种口只能插鼠标键盘，不能 ADB 控制

## USB ADB 输入测试

默认不会往安卓输入框里打字。

如果要测试中文输入，先在安卓上打开一个安全的空输入框，再运行：

```bash
sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode usb-adb --adb-input-test --adb-clipboard-text 李翔 --label adb_input
```

它会测试：

- `adb shell input text test123`
- clipboard service 写入中文
- `adb shell input keyevent 279` 粘贴

你现场要观察：

- `test123` 是否进入输入框
- `李翔` 是否进入输入框
- 是否只进入输入法剪贴板
- 是否需要额外点击输入框
- 医疗软件是否禁止粘贴

## USB ADB 文件读写测试

医院现场默认不要测试 ADB 文件写入。

原因：这一步会往安卓 `/sdcard/Download` 写测试文件。现场优先确认 ADB 截图、UI XML、中文输入即可。

如果以后明确允许测试文件写入，必须同时加确认参数：

```bash
sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode usb-adb --adb-file-test --allow-adb-file-write --label adb_file
```

如果只写 `--adb-file-test`，脚本会自动跳过，不会写安卓文件。

## 路线 B：网络 ADB

前提：

- RK3588 和安卓一体机在同一个局域网
- 安卓端开启网络 ADB 或无线调试

运行：

```bash
sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode net-adb --adb-target 192.0.2.20:5555 --label net_adb
```

如果成功，后续开发方式和 USB ADB 基本一致。

## 路线 C：HDMI 视觉

插线：

- 安卓一体机 HDMI OUT
- 接 RK3588 HDMI RX

运行：

```bash
sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode hdmi --label hdmi
```

它会收集：

- `/dev/video40` 是否存在
- v4l2 格式、时序、状态
- 多张 HDMI 截图：`hdmi_01.jpg`、`hdmi_02.jpg`、`hdmi_03.jpg`

判断：

- `HDMI visual: usable`：能走 HDMI 截屏 + 视觉识别
- 截图为空/很小：可能 HDMI 没输出、线不对、不是 HDMI OUT、分辨率/格式不支持

## 路线 D：虚拟 HID

插线：

- RK3588 USB device/gadget 口
- 接安卓一体机支持鼠标键盘的 USB Host/OTG 口

运行：

```bash
sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode hid --label hid
```

它会检查：

- `/dev/hidg0`
- `/dev/hidg1`
- `/dev/g_printer0`
- C0 UDC 是否 `configured`
- configfs 中键盘、鼠标、打印机 gadget 配置

判断：

- `Virtual HID: usable`：安卓已经枚举 RK3588 虚拟键盘鼠标
- `not_configured`：安卓没把 RK3588 当 USB 设备枚举，可能线口不对

## HID 输入测试

默认不会输入。

如果要测试英文数字输入，先在安卓上打开安全的空输入框：

```bash
sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode hid --hid-type-test --hid-text 123456 --label hid_type
```

如果输入框出现 `123456`，说明英文数字可走 HID。中文输入不能直接靠普通 HID 完成。

## HID 鼠标点击测试

如需测试绝对坐标点击：

```bash
sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode hid --hid-click 500,500 --label hid_click
```

注意：这会真的点击安卓屏幕坐标。

## 回来时打包结果

```bash
cd /var/lib/rk3588-gateway/android_site_probe
sudo tar -chzf /tmp/android_site_probe_latest.tgz latest
ls -lh /tmp/android_site_probe_latest.tgz
```

把这个文件拿回来：

```bash
/tmp/android_site_probe_latest.tgz
```

## 怎么判断最终方案

优先级：

1. `USB ADB: usable` 或 `Network ADB: usable`
   - 最优
   - 可以截图、读 UI XML、中文输入、读取文件

2. `HDMI visual: usable` + `Virtual HID: usable`
   - 可做
   - 走 HDMI 视觉识别 + HID 点击/英文数字输入
   - 中文输入需要额外方案

3. 只有 `HDMI visual: usable`
   - 可以看屏幕，但还缺输入控制

4. 只有 `Virtual HID: usable`
   - 可以输入/点击，但缺可靠读屏

5. ADB、HDMI、HID 都不可用
   - 无客户端自动化难度很高
   - 需要医院或厂商开放报告目录、接口、打印链路、网络共享或调试权限
