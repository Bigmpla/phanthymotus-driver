# Bumi 二维码读取卡片（qr_scan）

## 功能与边界

复用现有 CameraPlugin 的 JPEG 帧缓存（`/<namespace>/camera/color`），
不重新打开 RealSense、不发送运动指令，不访问或执行二维码中的内容。
所有可解码二维码返回文本、原始图像像素坐标、图像尺寸和帧接收时间。
只支持现有 Bumi 彩色相机；不是任意相机 topic 的通用订阅器。

卡片类型为 processor，输出 `/<namespace>/qr_scan/result`，格式 `data/json`。
在画布中添加已有 camera 和新 qr_scan，并将 camera 的 JPEG 输出连接到
qr_scan 输入。部署中的自动启动也会使用同一默认相机缓存。
`info` 返回权威输出 topic，`read` 读取最新结果，不触发一次额外扫描。

状态：`waiting_for_frame`、`detected`、`no_qr`、`stale`、`decode_error`、
`unavailable`、`error`、`idle`。`running` 只代表处理线程在运行，
不代表相机已有有效帧；以 `status` 判断数据是否可用。

## 配置与结果

在 config.yaml 的 plugins.qr_scan 中配置：

```yaml
qr_scan:
  enabled: true
  scan_hz: 2
  stale_after_s: 3
  rearm_after_s: 3
```

必须启用 `plugins.camera`。参数在驱动启动时读取；修改 YAML 后重新部署/启动。
不新增运行时配置表单。输入 topic 若由画布传入，必须与默认相机 topic 相同。

每次处理只读取最新帧，约 2 次/秒。长时间解码不会积压图像队列。
图像年龄以本机接收时间计算，不是厂商传感器的原始采样时间。

结果中 `codes` 表示当前帧的检测结果，`new_events` 表示本帧首次出现的内容。
同一内容持续被检测到时不重复发新事件；超过 rearm_after_s 未被检测到后再次
出现，会产生新事件。这是“未被观察到”的时间，不保证物理标签真的离开了画面。
两个相同内容的物理二维码保留两个位置，但合并为一个内容事件。
消费者使用 `(session_id, sequence)` 去重，不能把反复调用 read 当作新事件。
去重历史最多保留 256 项。stop 清空当前结果；start 开始新 session。

二维码位置是像素坐标，不是物体的三维位置。第一版不评估畸变后的空间位置，
不支持二进制载荷或 GS1 专门语义；OpenCV 无法解码的帧可能表现为 no_qr。
第一版不内置语音播报或决策核触发逻辑。

## 本地离线验证

运行于仓库根目录：

```bash
python3 -m venv .venv-qr
source .venv-qr/bin/activate
python -m pip install 'opencv-python-headless<5' numpy pytest pyyaml
python -m pytest -q tests/test_bumi_qr_scan.py
python noetix/bumi/qr_scan.py /path/to/sample.jpg
```

测试使用真实 OpenCV 解码真实 JPEG 中的英文、中文和多个二维码。
其他用例覆盖空白、无效 JPEG、去重、过期帧、启停和实际 bundle 注册。
ROS 发布、相机 SDK、ARM64 Docker 构建与画布接线尚需现场验证。

## 提交与部署

新增 qr_scan.py，并修改 main.py、config.yaml、driver.yaml、Dockerfile。
现有镜像已包含 numpy 和 opencv-python-headless，不新增运行时依赖。
Dockerfile 复制插件；driver.yaml 增加 marketplace 卡片声明。

在自己的 Fork 上提交这些文件、说明和测试，向主仓库创建 PR。
按入职文档在 PR 评论 `/request_bot_review`，待成功后使用机器人回复的
镜像与部署指令。不要在未知的工作目录直接覆盖现场配置；先核对原配置。

## Bumi 真机验收

1. 确认正确的 Bumi、登录权限、当前镜像和回退镜像。
2. 机器人保持静止。确认已有 camera 画面正常，部署后原卡片仍可用。
3. `tools/list` 应出现 qr_scan，画布中可添加并连接 camera 的 JPEG 输出。
4. 将测试标签 BUMI-001 放在镜头前：codes 内容完全一致，四角坐标在图像内。
5. 持续展示 10 秒：只有一次新内容事件；移开超过 3 秒再展示：出现一次新事件。
6. 两个不同标签同框：都能输出。重复内容标签的位置各自返回。
7. 正常文字、空白纸：不应输出任何虚构内容。
8. 通过测试发布器/相机维护操作制造断流：超过 3 秒应显示 stale，并清空旧码。
9. 相机恢复后应重新处理新帧。重复启停 qr_scan，原相机继续预览且无额外 worker。
10. 测量开启前后 CPU、相机帧率、处理延迟；保存实际结果，不将离线通过写成真机通过。

识别效果按“场景/距离/角度/照明/成功次数/尝试次数/延迟”记录。
建议用 10 cm 标签从 0.3、0.5、0.8 m 开始试验；这些是测试条件，不是支持范围承诺。
同时检查二维码在图像中的像素大小。距离太远、反光、运动模糊、弯曲标签均可能失败。
