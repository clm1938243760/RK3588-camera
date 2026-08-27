# 安卓一体机医院现场最终准备清单

日期：2026-06-11

目标：确认医院安卓一体机能否在“不安装客户端、不 root”的前提下，由 RK3588 自动完成读屏、输入、点击、取报告。

## 一、出发前必须带

- RK3588 板子和电源
- 笔记本电脑和电源
- 网线一根，最好两根
- 小交换机或便携路由器
- HDMI 线
- USB-A 转 USB-C
- USB-C 转 USB-C
- USB-A 转 Micro USB
- USB-A 公对公线，仅在确认接口安全时使用
- USB 鼠标、键盘
- U 盘
- 插排
- 手机拍摄完整流程视频

## 二、RK3588 上已准备好的工具

主脚本：

```bash
/opt/rk3588_gateway/scripts/android_site_probe.py
```

说明：

```bash
/opt/rk3588_gateway/ANDROID_SITE_PROBE.md
```

结果目录：

```bash
/var/lib/rk3588-gateway/android_site_probe
```

最新结果：

```bash
/var/lib/rk3588-gateway/android_site_probe/latest
```

## 三、出发前 RK3588 预检

板子接电、接网线后，在笔记本运行：

```powershell
$BOARD_IP = "192.0.2.10"
ping $BOARD_IP
ssh "linaro@$BOARD_IP"
```

进入板子后运行：

```bash
python3 /opt/rk3588_gateway/scripts/android_site_probe.py --print-plan
sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode board --label preflight
```

确认：

- 能生成 `report.txt`
- `adb version` 能输出
- `/dev/video40` 存在
- `/dev/hidg0`、`/dev/hidg1` 存在
- 磁盘剩余空间足够

检查命令：

```bash
command -v adb
adb version
ls -l /dev/video40 /dev/hidg0 /dev/hidg1 /dev/g_printer0
df -h
```

## 四、到医院现场的测试顺序

### 1. 先做总引导测试

```bash
sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode guided --label hospital
```

如果不想每一步按回车：

```bash
sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode guided --no-prompt --label hospital
```

### 2. 测 USB ADB

插线：

- 安卓一体机调试口、device 口或 OTG device 口
- 接 RK3588 USB Host 口

安卓端：

- 打开开发者选项
- 打开 USB 调试
- 出现 RSA 授权弹窗时点允许

运行：

```bash
sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode usb-adb --label usb_adb
```

成功标志：

- `USB ADB: usable`
- 有 `adb_*.png`
- 有 `adb_*_window.xml`
- report 里能看到 Android 版本、型号、分辨率、前台包名

### 3. 测 ADB 中文输入

先在安卓一体机打开一个安全的空输入框，再运行：

```bash
sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode usb-adb --adb-input-test --adb-clipboard-text 李翔 --label adb_input
```

现场观察：

- `test123` 是否进入输入框
- `李翔` 是否进入输入框
- 是否只进入输入法剪贴板
- 医疗软件是否禁止粘贴
- 是否需要额外点击输入框

### 4. 不测 ADB 文件读写

医院现场默认不要测试 ADB 文件写入，因为这一步会向安卓 `/sdcard/Download` 写测试文件。

只需要确认：

- ADB 截图是否成功
- UI XML 是否成功
- 中文输入是否成功
- 医疗软件是否允许粘贴

如果以后明确允许测试文件写入，必须同时加确认参数：

```bash
sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode usb-adb --adb-file-test --allow-adb-file-write --label adb_file
```

### 5. 测 HDMI 输出

插线：

- 安卓一体机 HDMI OUT
- 接 RK3588 HDMI RX

运行：

```bash
sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode hdmi --label hdmi
```

成功标志：

- `HDMI visual: usable`
- 结果目录里有 `hdmi_01.jpg`、`hdmi_02.jpg`、`hdmi_03.jpg`
- 图片能看到安卓医疗软件界面

### 6. 测虚拟 HID

插线：

- RK3588 USB device/gadget 口
- 接安卓一体机 USB Host/OTG 口

运行：

```bash
sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode hid --label hid
```

成功标志：

- `Virtual HID: usable`
- `c0_state=configured`
- `/dev/hidg0`、`/dev/hidg1` 存在

测试英文数字输入：

```bash
sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode hid --hid-type-test --hid-text 123456 --label hid_type
```

## 五、必须向医院或厂商问清楚

- 安卓版本是多少
- 是否允许长期打开 USB 调试
- USB 调试授权重启后是否保留
- 是否支持网络 ADB
- 哪个 USB 口是调试/device 口，哪个只是 Host 口
- 是否有 HDMI OUT
- 医疗软件是否全屏或 kiosk 模式
- 是否会锁屏、休眠、自动升级
- 是否能导出 PDF、图片或原始报告文件
- 报告默认保存目录在哪里
- 是否支持 U 盘导出
- 是否能通过共享目录、FTP、HTTP、Samba 或打印方式输出报告
- 是否允许 RK3588 长期连接设备

## 六、回来必须带回

打包最新结果：

```bash
cd /var/lib/rk3588-gateway/android_site_probe
sudo tar -chzf /tmp/android_site_probe_latest.tgz latest
ls -lh /tmp/android_site_probe_latest.tgz
```

需要带回：

```bash
/tmp/android_site_probe_latest.tgz
```

另外手机拍摄：

- 一体机外观和接口
- 医疗软件打开方式
- 新建病人流程
- 输入病人信息流程
- 检查开始和完成状态
- 保存、打印、导出报告流程
- 异常弹窗

## 七、路线判断

优先级：

1. USB ADB 或网络 ADB 可用
   - 最优路线
   - 可以截图、读 UI XML、中文输入、访问文件

2. HDMI 可用 + HID 可用
   - 可做视觉识别和点击
   - 英文数字可输入
   - 中文输入需要额外方案

3. 只有 HDMI 可用
   - 能读屏，但缺控制

4. 只有 HID 可用
   - 能点击和输入英文数字，但缺读屏

5. ADB、HDMI、HID 都不可用
   - 无客户端自动化难度很高
   - 需要医院或厂商开放接口、报告目录、打印链路或调试权限
