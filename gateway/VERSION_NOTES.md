# RK3588 Debian Bridge Version Notes

## Current Version

- Version: `v0.913.88`
- Target board: ATK-DLRK3588 / RK3588 Debian
- Repository: `clm1938243760/RK3588-Debian-bridge`
- Runtime path on board: `/opt/rk3588_gateway`
- Runtime state path: `/var/lib/rk3588-gateway`
- Python package version: `0.913.88`

## v0.913.88 - RK3588 Local Vision Stabilization

- Stabilized the RK3588 body-composition visual flow after HDMI RX migration.
- Added tolerant start-check detection when OCR sees the toolbar button but misses the ready text.
- Added a visual green-progress-bar fallback for the completed-check state.
- Added noisy PDF prompt recognition for RKNN OCR results such as `PDF...报告` plus `是(Y)`.
- Added stale dialog cleanup before starting a new scan, including residual PDF prompts and report-generated dialogs.
- Added a visual report-generated dialog fallback using the information icon, confirm button frame, and dialog background checks.
- Verified `P2605260007` end-to-end on RK3588: form input, start check, data analysis, PDF confirmation, report capture, and upload success.

## v0.912.88 - RK3588 Vision Migration

- Ported the latest RK3568 vision flow into the RK3588 project, including body-composition and BodyPass profiles.
- RK3588 default capture path now uses HDMI RX `/dev/video40` with BGR 1920x1080@60 and a single stable frame.
- Vision endpoints default to the board-local service: `http://127.0.0.1:5002/icon/locate` and `http://127.0.0.1:5002/window/detect`.
- Added local RKNN vision service unit: `rk3588-ppocr.service`.
- Added install script wiring so the USB gadget service, local vision service, and gateway service are installed and restarted together.
- Disabled GPIO selection in the RK3588 flow: when multiple application items are returned, the gateway auto-selects the matched item, otherwise the first item.
- Added RK3588 profile files for `body_composition` and `bodypass`.
- Kept UVC/MJPG capture support as a configurable fallback for older capture-card deployments.

## Version Scope

`v0.912.88`保存的是 RK3588 当前可运行版本。这个版本以原 RK3568 业务流程为基础，加入 RK3588 板子的 USB gadget、双 UDC、HDMI RX 截图、本地视觉服务和视觉点击流程。

## Main Functions

1. USB gadget
   - C0 `fc000000.usb`: HID keyboard, HID mouse, USB printer.
   - C1 `fc400000.usb`: mass storage.
   - U 盘功能单独占用一个 UDC，HID 和 printer 使用另一个 UDC。
   - 保持 C0 function 顺序为 keyboard -> mouse -> printer。

2. Scanner and patient API
   - 扫码枪输入病人体检号。
   - 调用体检系统接口查询患者信息。
   - 根据检查项目判断是否进入人体成分检查自动录入流程。

3. HID automatic input
   - 通过 `/dev/hidg0` 模拟键盘输入。
   - 通过 `/dev/hidg1` 模拟绝对坐标鼠标点击。
   - 使用 `MarkInfo_SearchTitle_Config_100.json` 描述录入字段和确认点击流程。

4. Printer and report upload
   - 通过 `/dev/g_printer0` 接收 Windows 打印数据。
   - 监听 mass storage 中的报告文件。
   - 上传成功后执行实体打印；上传失败不触发实体打印。

5. HDMI RX and vision flow
   - HDMI RX 视频节点: `/dev/video40`。
   - 截图格式: 1920x1080 JPEG。
   - 视觉接口:
     - `POST /icon/locate`: 识别桌面软件图标并返回坐标。
     - `POST /window/detect`: 识别人体成分软件窗口状态和 OCR 坐标。
   - 线性流程:
     - 未检测到窗口时打开软件。
     - `label0` 登录窗口点击“登录”。
     - `label1` 主窗口根据 OCR 点击“新建患者”、“开始检查”、“数据分析”。
     - `label2` 新建患者窗口执行 HID 表单录入。
     - `label4` PDF 生成提示中选择包含“是”的按钮。
     - `label5` 报告生成完成后点击“确定”，再回到主窗口点击“新建患者”结束任务。

## Kernel and Board Notes

- RK3588 必须关闭 `CONFIG_USB_CONFIGFS_UEVENT`。该选项开启时，复合 gadget 枚举可能进入 Rockchip/Android `android_setup()` 并触发内核崩溃。
- `/dev/hidg*` 和 `/dev/g_printer*` 编号依赖 configfs gadget 清理状态。异常时先清理 stale gadget，再启动正式 gadget service。
- C1 mass storage 绑定失败 `Device or resource busy` 时，优先检查是否存在旧 gadget 仍绑定 `fc400000.usb`。

## Services

- USB gadget service: `rk3588-usb-printer-gadget.service`
- Local vision service: `rk3588-ppocr.service`
- Main service: `rk3588-gateway.service`
- Local API: `0.0.0.0:8080`

## Validation Commands

```bash
systemctl status rk3588-usb-printer-gadget.service rk3588-gateway.service --no-pager
find /sys/kernel/config/usb_gadget -maxdepth 3 -name UDC -print -exec cat {} \;
ls -l /dev/hidg* /dev/g_printer*
curl http://127.0.0.1:8080/health
curl -sS -X POST http://127.0.0.1:8080/scan \
  -H "Content-Type: application/json" \
  -d '{"code":"P2605260007"}'
```
