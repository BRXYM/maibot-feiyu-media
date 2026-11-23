# 肥鱼会话媒体回复

## 本地修改说明（2026-11-24）

好友名称有歧义时列出候选，支持按序号确认；确认后接着执行原请求。引用语音会结合消息类型和可用源文件选择发送方式。实现见 `contact_choice.py`、`quoted_delivery.py` 和 `proactive.py`。

用户明确教过的接话方式保存在对应私聊，遇到相似场景可召回。默认人设在平常请求中会复查“啧，来了来了”等不耐烦的开场。实现见 `reply_lessons.py`、`quality.py` 和 `plugin.py`。

对应功能提交 `7410ea6`。[公开版本 v1.4.7](https://github.com/BRXYM/maibot-feiyu-media/releases/tag/v1.4.7)

基于 QQ 私聊会话的语音、表情与本地生图工具。

## 安装与使用

解压 Release 到麦麦的 `plugins/feiyu-media/`，目录内应直接包含 `_manifest.json` 和 `plugin.py`。

需已有 QQ OneBot 适配器，以及与本插件配套的本地 feiyu-tools 运行目录。运行目录需要提供 server.py 和对应的发送、TTS、表情与 Pixiv 服务。生图另需同级 FeiyuDiffusion 中的 stable-diffusion.cpp、AWPainting、VAE 及可选 IP-Adapter。本 Release 提供插件源代码，运行时与权重需要另行配置。

在麦麦启动环境设置 FEIYU_OWNER_QQ 和 FEIYU_BOT_QQ 为自己的 QQ 和机器人 QQ，再启用插件。原有本地实例应留用 local.feiyu-media 以继续识别旧插件数据。

候选好友消歧、引用消息预览与源文件重发、私聊接话纠正和回复语气检查均依赖当前会话。请把 requirements.txt 的依赖安装到插件运行环境。

## 许可证

插件代码采用 MIT。第三方运行时及模型采用各自许可。
