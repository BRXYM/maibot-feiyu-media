# 肥鱼会话媒体回复

基于 QQ 私聊会话的语音、表情与本地生图工具。

## 安装与使用

解压 Release 到麦麦的 `plugins/feiyu-media/`，目录内应直接包含 `_manifest.json` 和 `plugin.py`。

需已有 QQ OneBot 适配器，以及与本插件配套的本地 feiyu-tools 运行目录。运行目录需要提供 server.py 和对应的发送、TTS、表情与 Pixiv 服务。生图另需同级 FeiyuDiffusion 中的 stable-diffusion.cpp、AWPainting、VAE 及可选 IP-Adapter。本 Release 提供插件源代码，运行时与权重需要另行配置。

在麦麦启动环境设置 FEIYU_OWNER_QQ 和 FEIYU_BOT_QQ 为自己的 QQ 和机器人 QQ，再启用插件。原有本地实例应留用 local.feiyu-media 以继续识别旧插件数据。

## 许可证

插件代码采用 MIT。第三方运行时及模型采用各自许可。
