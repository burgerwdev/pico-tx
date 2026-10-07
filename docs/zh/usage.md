# 使用说明

> 中文 · [English](../en/usage.md)

## 烧录

1. 按住 **BOOTSEL** 按钮的同时把 Pico 插入电脑 USB，出现 `RPI-RP2` 盘；
2. 把 `firmware/rp2040pico_fm_firmware.uf2` 拖入该盘（自动烧录并重启）；
   或 `tools/flash.sh`（需 picotool）。
3. 校验（可选）：在仓库根目录运行 `sha256sum -c firmware/sha256.txt`。

## 连接

- 设备会枚举为：**串口**（MicroPython REPL / `fm>` 控制台）+ **USB 音频设备**
  （"RP2040 RF Transmitter"，系统里显示为 RP2040 RF Transmitter / BurgerW）。
- 推荐：`tools/serial.sh`（tio）——切频段/预设会重启板子，tio 会自动重连；
  配合 udev 规则把设备名固定为 `/dev/pico`，ttyACM 序号不再漂移。
- 手动：Linux `screen /dev/ttyACM0 115200`；Windows PuTTY（COM 口 115200）；
  Thonny 也可直接连接。
- 上传控制台脚本（只需一次）：`tools/upload.sh`（直接 `mpremote cp` 会因
  `fm>` 控制台占用 REPL 而失败）。
- 之后拔插或按 RESET，等待 2~3 秒，打开串口即自动进入 `fm>` 控制台。

## 播放

1. 在电脑声音设置中把输出设备选为 **RP2040 RF Transmitter**；
2. 播放音乐；
3. FM 收音机贴近 Pico，调到 **87.9MHz**。

> 提示：默认开启 75µs 预加重（与收音机去加重匹配），声音更清晰；若觉得失真，
> 可 `pre off` 对比、`dev 40000` 收窄频偏、或把电脑音量降到 60~80%。

## 频段预设与手台

```
band fm    98.0 MHz     广播 FM（载波停靠静音，15kHz 音频）
band 2m    145.0 MHz    2m 业余段 — 手台 VHF，WIDE 模式
band 409   409.75 MHz   免证公众对讲机，3 次谐波（需滤波*）
band 433   433.92 MHz   ISM/业余，3 次谐波 — 手台 UHF，WIDE 模式
band 446   446.00625 MHz  PMR446 ch1，3 次谐波（欧洲）
```

每个预设自动设置载波/频偏、PLL 参考分频（凡在静音时关断 RF 的频段都用
refdiv 2——PDM 步长减半是窄带/谐波链路音质干净的关键；广播 FM 保持 refdiv 1，
因为停靠载波在 refdiv 2 下会有可听嗡声）、静音模式和 NFM 语音音频链（300Hz 高通 + 3kHz
低通），然后重启。手台注意事项：

- 把手台调到**实际**发射频率（比标称偏几 kHz —— 晶振误差 × 谐波次数）。
  `trim <±Hz>` 可补偿（如 `trim -5000`），`trim 0` 取消；
- UHF 段音频已做语音带整形（300Hz 高通 + 3kHz 低通），预加重请用
  `pre 300` 匹配手台 300µs 去加重；
- 无声时 RF 输出会键控关断（静噪闭合 → 静音）。`silence park` 恢复广播行为；
- \* 409MHz 的基频在 136.6MHz，位于民航频段（118-137MHz）内且功率比 409
  信号更强——没有 409 带通滤波器抑制基频前，请勿辐射。

## FM 静音最佳实践

未调制停靠载波是 FM 收音机静音的机制（暂停 / `mute on` / `audio off`）。
若静音时上位机仍在发流，USB 包接收的片上数字活动会向停靠载波耦合轻微噪声
（芯片固有，原版固件相同）。要完全静音请暂停上位机播放。窄带频段不受影响
（RF 直接键控关断）。

## 播放

1. 在电脑声音设置中把输出设备选为 **RP2040 RF Transmitter**；
2. 播放音乐；
3. FM 收音机贴近 Pico，调到 **87.9MHz**。

> 提示：默认开启 75µs 预加重（与收音机去加重匹配），声音更清晰；若觉得失真，
> 可 `pre off` 对比、`dev 40000` 收窄频偏、或把电脑音量降到 60~80%。

## 指示灯

- 默认（普通 LED 板）`led 3`：随音频电平闪动（VU）；`led 0/1/2`：关/常亮/发射时常亮；
- 板载灯不是 GPIO25 的板子（如 RP2040-Zero 的 WS2812 在 GPIO16）：
  `ledpin ws2812` 启用彩灯模式（`led 3` = 绿→黄→红 VU），或接普通 LED 后用
  `ledpin <gpio>` 指定。

## 切换频段

一键预设：`band fm` / `band 2m` / `band 409` / `band 433` / `band 446`。
自定义：`reinit 98000000 50000`（载波/频偏，可加第三个参数换引脚）→ 保存并
重启生效；`freq <Hz>` 在当前 PLL 频带内实时微调，范围外自动重启到该载波；
`trim <±Hz>` 补偿晶振偏移。
