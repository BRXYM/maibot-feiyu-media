"""Private-chat media tools with host-provided conversation routing."""
from pathlib import Path
from datetime import datetime, timezone
import importlib.util
import asyncio
import json
import re
import secrets
import sqlite3
import time
import sys
import uuid

import httpx
from maibot_sdk import MaiBotPlugin, Tool, ConfigWidget, ConfigWidgetState, HookHandler
from maibot_sdk.types import ToolParameterInfo, ToolParamType, HookMode, HookOrder
from .proactive import BOT, OWNER, ProactiveMixin
from .pixiv import PixivMixin, INSTRUCTION, latest_incoming, validate_pixiv_image
from .diffusion import DEFAULT_STICKER, FEIYU_FRAMING, OUTPUT as DIFFUSION_OUTPUT, generate_image, resolve_sticker
from .quality import AGREEMENT, BRUSHOFF, ECHO, HURRY_TAIL, IMAGE_REVISIONS, ordinary_request_reply, quality_report, record_quality, reply_drift, requested_style, request_tone_reason, review_image
from .reply_lessons import ReplyLessons

TOOLS = Path(__file__).resolve().parents[2] / 'feiyu-tools'
sys.path.insert(0, str(TOOLS))
spec = importlib.util.spec_from_file_location('feiyu_media_backend', TOOLS / 'server.py')
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


def parameter(name, description, required=True, kind=ToolParamType.STRING):
    return ToolParameterInfo(name=name, param_type=kind, description=description, required=required)


def is_status_question(text):
    text = re.sub(r'[\s，。？?！!～~]', '', text)
    return bool(re.fullmatch(r'(?:你)?(?:现在|这会儿|此刻|今天)?(?:正?在|都在)?(?:干嘛|干吗|干什么|做什么|忙什么|干啥|忙啥|做啥|状态如何)(?:呢|呀|啊|吗)?', text))


def has_current_reply(messages, incoming, quote):
    replies = [row for row in messages
               if str(row.get('message_info', {}).get('user_info', {}).get('user_id', row.get('user_id', ''))) == BOT
               and row.get('processed_plain_text') and not row.get('is_notify')
               and float(row['timestamp']) > float(incoming['timestamp'])]
    replies.sort(key=lambda row: float(row['timestamp']))
    return any(quote in row['processed_plain_text'] for row in replies[-3:])


class FeiyuMediaPlugin(PixivMixin, ProactiveMixin, MaiBotPlugin):
    _drawing_task: asyncio.Task | None = None
    _drawing_started: float | None = None
    _scene_message: str | None = None

    async def sticker_context(self,session_id, *, include_drawing=False):
        streams=await self.ctx.chat.get_private_streams(platform='qq')
        stream=next((s for s in streams if s.get('session_id')==session_id and not s.get('group_id')), None)
        if stream is None:
            return ''
        content = ('【表情使用偏好】闲聊中适合用表情接梗、撒娇、开心或无语时，可以自己决定发一张，不必等用户点名。'
                '优先大肥鱼扩展库：用send_qq_sticker(name=情绪或表情名)搜索并真正发送。工具未出现在当前列表时先tool_search查找send_qq_sticker。'
                '不要默认只用原生send_emoji的少量图片。扩展库没有合适图时再考虑原生图库。不要每轮发、连续刷屏或用文字括号假装发图；只有sent=true和message_id才说发了。\n'+INSTRUCTION)
        if include_drawing and str(stream.get('user_id')) == OWNER:
            content += ('\n【日常配图】跟主人聊天时，当前情绪或场景有独特的画面，可以顺手用share_feiyu_scene补一张同人插画。'
                        '比如被夸得有点得意、聊到窗边下雨想画自己的小桌子。普通接梗优先表情包。先接对方的话，同一轮选一种图即可。'
                        '对方问“你现在在干嘛”“你在忙什么”时，先结合当下生活状态用reply正常回答。回复送出后，如果此刻状态适合画，再在下一轮调用'
                        'share_feiyu_scene(kind=status, state_reply=刚发的状态原话片段, detail=把同一状态变成画面)。'
                        '例如刚说“正窝着发懒”，就画醒着的自己懒洋洋窝在沙发上。过去活动、旧图题材和下一步计划都不能当成现在。'
                        '只有对方开口点名画图才用generate_qq_image。配图是角色小剧场，后台发图，不必预告或等待；跳过时不重试。')
        if include_drawing and str(stream.get('user_id')) == OWNER:
            content += ('\n主人提到 QQ 好友时用 proactive_qq_contacts 查询原称呼。候选名单由工具发出，等主人选择，回序号用 choose_qq_recipient()。'
                        '引用消息托发或按歌名让你把唱过的歌发给某人，都调用 send_quoted_qq_message()；按歌名重发无需引用。'
                        '托发文字用 proactive_qq_message(on_request=true)，重新点歌用 generate_feiyu_song。按回执回复，参数未变时不要重复失败调用。')
            try:
                hint = await asyncio.wait_for(self.friend_hint(session_id),timeout=2)
            except (OSError, RuntimeError, ValueError, TimeoutError):
                hint = ''
            if hint:
                content += '\n' + hint
        if str(stream.get('user_id')) == OWNER:
            if not include_drawing:
                content += '\n托发引用消息时按本轮工具回执答复；失败只说明本次具体原因。'
            try:
                lesson = await self.reply_lessons.reference(session_id, capture=include_drawing)
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
                self.ctx.logger.warning('本轮未能读取私聊接话纠正：%s', type(error).__name__)
            else:
                if lesson:
                    content += '\n' + lesson
        return content

    @HookHandler('maisaka.planner.before_request',name='feiyu_sticker_preference',mode=HookMode.BLOCKING,order=HookOrder.LATE,timeout_ms=6500)
    async def sticker_planner(self,**kwargs):
        content=await self.sticker_context(str(kwargs.get('session_id','')), include_drawing=True)
        if not content:
            return {'action':'continue'}
        if kwargs.get('item_schema_version')!=1 or not isinstance(kwargs.get('items'),list):
            raise ValueError('表情偏好注入要求Context Item schema 1')
        item={'item_type':'UserMessageItem','meta':{'item_id':uuid.uuid4().hex,'logical_turn_id':None,'timestamp':datetime.now(timezone.utc).isoformat()},'parts':[{'type':'text','text':content}]}
        return {'action':'continue','modified_kwargs':{**kwargs,'items':[*kwargs['items'],item]}}

    @HookHandler('maisaka.planner.after_response',name='feiyu_execute_before_ack',mode=HookMode.BLOCKING,order=HookOrder.LATE,timeout_ms=1000)
    async def execute_before_ack(self, **kwargs):
        items = kwargs.get('output_items', [])
        # The host stops the batch at reply(wait), including the implicit default.
        # Let the next planner cycle compose its reply with the actual tool result.
        pending_action = False
        kept = []
        for item in reversed(items):
            call = item.get('tool_call', {}) if item.get('item_type') == 'FunctionCallItem' else {}
            name = call.get('func_name')
            if name in ('generate_feiyu_song','proactive_qq_contacts','proactive_qq_message','forward_quoted_qq_voice','send_quoted_qq_message','choose_qq_recipient'):
                pending_action = True
            if pending_action and name == 'reply' and call.get('args',{}).get('after_reply','wait') == 'wait':
                continue
            kept.append(item)
        if len(kept) == len(items):
            return {'action':'continue'}
        return {'action':'continue','modified_kwargs':{**kwargs,'output_items':list(reversed(kept))}}

    @HookHandler('maisaka.replyer.before_request',name='feiyu_sticker_reply_style',mode=HookMode.BLOCKING,order=HookOrder.LATE,timeout_ms=3000)
    async def sticker_replyer(self,**kwargs):
        content=await self.sticker_context(str(kwargs.get('session_id','')))
        return {'action':'continue','modified_kwargs':{**kwargs,'extra_prompt':str(kwargs.get('extra_prompt',''))+'\n'+content}} if content else {'action':'continue'}

    @HookHandler('maisaka.replyer.after_response', name='feiyu_persona_quality',
                 mode=HookMode.BLOCKING, order=HookOrder.LATE, timeout_ms=4000)
    async def check_reply_style(self, **kwargs):
        draft = str(kwargs.get('response') or '')
        session = str(kwargs.get('session_id') or '')
        message_id = str(kwargs.get('reply_message_id') or '')
        if self.reply_lessons.trim_question(session, message_id, draft) and draft.strip() != '？':
            return {'action': 'continue', 'modified_kwargs': {**kwargs, 'response': '？'}}
        reason = reply_drift(draft)
        tone_candidate = (AGREEMENT.match(draft.strip()) or HURRY_TAIL.search(draft.strip())
                          or BRUSHOFF.search(draft.strip()) or ECHO.fullmatch(draft.strip()))
        if not session or not (reason or tone_candidate):
            return {'action': 'continue'}
        streams = await self.ctx.chat.get_private_streams(platform='qq')
        stream = next((record for record in streams if record.get('session_id') == session and not record.get('group_id')), None)
        if stream is None:
            return {'action': 'continue'}
        current = await self.ctx.personality.get_current()
        if current.get('name') not in {'嘴臭鲸鱼 · 自动反击版', 'DeepSeek 小鲸鱼 · QQ 群友版（默认）'}:
            return {'action': 'continue'}
        messages = await self.ctx.message.get_recent(session, limit=16)
        if message_id:
            original = next((row for row in messages if str(row.get('message_id')) == message_id), None)
        else:
            try:
                original = latest_incoming(messages, str(stream['user_id']))
                if not 0 <= time.time() - float(original['timestamp']) <= 180:
                    return {'action': 'continue'}
                message_id = str(original['message_id'])
            except (TypeError, KeyError, ValueError):
                return {'action': 'continue'}
        if original is None or requested_style(str(original.get('processed_plain_text') or '')):
            return {'action': 'continue'}
        ordinary = current.get('name') == 'DeepSeek 小鲸鱼 · QQ 群友版（默认）' and tone_candidate
        if ordinary and not reason:
            reason = request_tone_reason(draft, str(original.get('processed_plain_text') or ''))
        if ordinary and not reason:
            previous = [row for row in messages if not row.get('is_notify') and
                        str(row.get('message_info',{}).get('user_info',{}).get('user_id',row.get('user_id',''))) == BOT and
                        float(row['timestamp']) < float(original['timestamp'])]
            previous.sort(key=lambda row:float(row['timestamp']),reverse=True)
            repeated = any(AGREEMENT.match(str(row.get('processed_plain_text') or '').strip()) for row in previous[:5])
            revised = ordinary_request_reply(draft, str(original.get('processed_plain_text') or ''), repeated)
            if revised != draft and revised:
                return {'action':'continue','modified_kwargs':{**kwargs,'response':revised}}
            if not revised:
                reason = '只剩惯用应声或无端催促玩笑，请直接接对方的具体请求'
        if not reason:
            return {'action':'continue'}
        key = session + ':' + message_id
        if kwargs.get('retry_count', 0) != 0:
            await self._record_quality('reply', key, '仍偏离')
            return {'action': 'continue'}
        await self._record_quality('reply', key, reason)
        return {'action': 'continue', 'modified_kwargs': {
            **kwargs, 'retry': True, 'retry_reason': f'刚才的回复出现{reason}。保留事实和要做的事，'
            f'按当前人设「{current["name"]}」像微信或 QQ 熟人聊天那样自然重说，直接接对方眼前这句话。',
        }}

    def _quality_path(self):
        return Path(self.ctx.paths.data_dir) / 'output-quality.sqlite3'

    async def _record_quality(self, kind, key, outcome):
        try:
            await asyncio.to_thread(record_quality, self._quality_path(), kind, key, outcome)
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
            self.ctx.logger.warning('内容自检结果未写入：%s', type(error).__name__)

    @Tool('feiyu_output_quality', platforms='qq', chat_scope='private',
          description='主人问你最近画图、说话或唱歌为什么有问题时，查询本地自检统计。区分合成失败、投递不明和未通过验收。')
    async def output_quality(self, **context):
        try:
            source, _ = await self.social_manager().source(context)
        except ValueError:
            return {'success': False, 'content': '无法确认当前私聊归属。'}
        if str(source['user_id']) != OWNER:
            return {'success': False, 'content': '这份本地汇总仅在主人私聊中可用。'}
        report = await asyncio.to_thread(quality_report, self._quality_path(), TOOLS/'audio/singing-jobs')
        return {'success': True, 'content': json.dumps(report, ensure_ascii=False)}

    config_widgets = [
        ConfigWidget(id='social_status', kind='status', label='主动联系人'),
        ConfigWidget(id='social_check', kind='action', label='检查QQ联系人连接'),
        ConfigWidget(id='pixiv_check', kind='action', label='检查视觉找图接入'),
        ConfigWidget(id='quality_status', kind='status', label='内容自检'),
    ]

    async def get_config_widget_state(self):
        state = await self.social_manager().load()
        text = f"已选择 {len(state['contacts'])} 位主动联系人；初始联系人为{OWNER}。"
        text += getattr(self, '_social_check_summary', '') + getattr(self, '_pixiv_check_summary', '')
        report = await asyncio.to_thread(quality_report, self._quality_path(), TOOLS/'audio/singing-jobs')
        counters = [('生图', 'image'), ('语音', 'speech'), ('人设重写', 'reply'), ('最近学唱', 'singing')]
        quality = '；'.join(f'{label} ' + ('、'.join(f'{k} {v}' for k, v in report.get(name, {}).items()) or '暂无记录')
                           for label, name in counters)
        return {'social_status': ConfigWidgetState(text=text, tone='success'), 'social_check': ConfigWidgetState(),
                'pixiv_check': ConfigWidgetState(), 'quality_status': ConfigWidgetState(text=quality)}

    async def on_config_widget_action(self, action_id):
        if action_id == 'pixiv_check':
            self._pixiv_check_summary = await self.diagnose_pixiv_preflight()
            self.ctx.logger.info(self._pixiv_check_summary)
            return
        if action_id != 'social_check':
            raise ValueError('未知操作')
        social = self.social_manager()
        streams = await self.ctx.chat.get_private_streams(platform='qq')
        people = await social.directory(streams)
        state = await social.load()
        activity = await social.activity('today')
        self._social_check_summary = f" QQ连接正常，能识别 {len(people)} 位好友/已有私聊对象；今天有 {len(activity['recent_conversations'])} 位用户发过私聊。主动名单人数不等于聊天人数。"
        report = {'bot_id': state['bot_id'], 'known_people_count': len(people), 'chosen_contact_count': len(state['contacts']), 'owner_known': OWNER in people, 'send_attempted': False, 'recent_conversations': activity['recent_conversations']}
        path = social.path.with_name('social-connection-check.json')
        await asyncio.to_thread(path.write_text, json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    async def on_load(self):
        if not OWNER.isdecimal() or not BOT.isdecimal() or OWNER == BOT:
            raise ValueError("请先设置互不相同的 FEIYU_OWNER_QQ 与 FEIYU_BOT_QQ")
        self.ctx.logger.info('肥鱼媒体工具已启用：收件人绑定当前QQ私聊，不使用固定QQ号')
        self.reply_lessons = ReplyLessons(self.ctx, OWNER)
        social = self.social_manager()
        async with social.lock:
            await social.save(await social.load())

    async def on_unload(self):
        if self._drawing_task and not self._drawing_task.done():
            self._drawing_task.cancel()
            await asyncio.gather(self._drawing_task, return_exceptions=True)

    async def on_config_update(self, config):
        pass

    async def _invoke_media(self, operation, arguments, context):
        result = await self._execute_media(operation, arguments, context)
        return {**result, 'success': not bool(result.get('error')), 'content': json.dumps(result, ensure_ascii=False)}

    async def _invoke_voice(self, operation, arguments, context):
        try:
            result = await self._execute_media(operation, arguments, context)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            result = {'sent': False, 'status': '服务不可用', 'error': '肥鱼语音后端当前没有响应'}
        except (httpx.HTTPError, TimeoutError):
            result = {'sent': False, 'status': '请求异常', 'error': '语音请求未得到完整回执'}
        if result.get('pending'):
            result['note'] = '音频还在处理，只能查询原任务，别重新提交。'
        else:
            if result.get('sent') and result.get('message_id'):
                outcome = '已发送'
            else:
                state = 'rejected' if result.get('retcode') is not None else result.get('status')
                outcome = {'delivery_unknown': '投递不明', 'unknown': '投递不明', 'failed': '合成失败',
                           'interrupted': '合成中断', '服务不可用': '服务不可用', '请求异常': '请求异常',
                           'rejected': '投递失败'}.get(state, '参数或投递失败')
                result.update(stop_retry=True, note='本轮停止语音重试，用文字正常回应；投递不明时不要重发原音频。')
            if not result.get('duplicate_suppressed'):
                await self._record_quality('speech', result.get('job_id') or uuid.uuid4().hex, outcome)
        success = bool(result.get('pending') or (result.get('sent') and result.get('message_id')))
        return {**result, 'success': success, 'content': json.dumps(result, ensure_ascii=False)}

    async def _execute_media(self, operation, arguments, context):
        # Resolve the actual registered chat rather than asking the LLM for a QQ number.
        stream_id = str(context.get('stream_id') or '').strip()
        if not stream_id or context.get('group_id') or context.get('platform') != 'qq':
            return {'sent': False, 'error': '媒体回复需要当前QQ私聊上下文，拒绝猜测收件人'}
        streams = await self.ctx.chat.get_private_streams(platform='qq')
        if not isinstance(streams, list):
            return {'sent': False, 'error': '无法查询当前QQ私聊会话'}
        matches = [s for s in streams if s.get('session_id') == stream_id]
        if len(matches) != 1:
            return {'sent': False, 'error': '当前私聊未注册或不唯一，拒绝发送'}
        stream = matches[0]
        target = str(stream.get('user_id') or '')
        if stream.get('group_id') or stream.get('platform') != 'qq' or stream.get('is_group_session'):
            return {'sent': False, 'error': '只支持当前QQ私聊'}
        if context.get('user_id') and str(context['user_id']) != target:
            return {'sent': False, 'error': '会话与发言人不匹配，拒绝发送'}
        route = {'target': target, 'session_id': stream_id}
        token = backend.MEDIA_ROUTE.set(route)
        try:
            backend.current_media_route()
            return await getattr(backend, operation)(**arguments)
        except (ValueError, FileNotFoundError) as exc:
            return {'sent': False, 'error': str(exc)}
        finally:
            backend.MEDIA_ROUTE.reset(token)

    @Tool('send_feiyu_voice', platforms='qq', description='用肥鱼专用声线给当前私聊对象发送语音。pending时只用feiyu_voice_job查询原任务。stop_retry=true时本轮改用文字。', parameters=[parameter('text', '正文1至300字'), parameter('style', 'cute可爱（默认）或gentle温柔', False)])
    async def voice(self, text='', style='cute', **kwargs):
        return await self._invoke_voice('send_feiyu_voice', {'text': text, 'style': style}, kwargs)

    @Tool('feiyu_voice_job', platforms='qq', description='继续查询当前私聊提交的语音任务，并只向原会话发送一次。不能查询或发送别人的任务。', parameters=[parameter('job_id', '先前返回的语音任务编号')])
    async def job(self, job_id='', **kwargs):
        return await self._invoke_voice('feiyu_voice_job', {'job_id': job_id}, kwargs)

    @Tool('send_qq_voice', platforms='qq', description='合成语音并发送给当前私聊对象。默认feiyu-cute，可选feiyu-gentle或明确指定Windows音色。', parameters=[parameter('text', '正文1至300字'), parameter('voice', '默认feiyu-cute，可选feiyu-gentle', False), parameter('rate', '默认0，肥鱼专用声线仅支持0', False, ToolParamType.INTEGER)])
    async def system_voice(self, text='', voice='feiyu-cute', rate=0, **kwargs):
        return await self._invoke_voice('send_qq_voice', {'text': text, 'voice': voice, 'rate': rate}, kwargs)

    @Tool('send_qq_audio', platforms='qq', description='把已有本地音频作为QQ语音发送给当前私聊对象；只有sent=true和message_id才表示发送成功。', parameters=[parameter('audio_path', '本地音频完整路径')])
    async def audio(self, audio_path='', **kwargs):
        return await self._invoke_media('send_qq_audio', {'audio_path': audio_path}, kwargs)

    @Tool('send_feiyu_song', platforms='qq', description='仅在用户明确要求重播旧试听时使用的缓存重播工具；目前为小星星第一句。普通唱歌、换首歌、随便唱一首，都默认用generate_feiyu_song自行选歌并学唱，不要调用本缓存工具代替学习。', parameters=[parameter('song', '用户明确要求重播的缓存曲名')])
    async def song(self, song='', **kwargs):
        if not song.strip():
            return {'success':False,'sent':False,'error':'默认请用generate_feiyu_song学习新歌；重播缓存必须明确曲名。'}
        return await self._invoke_media('send_feiyu_song', {'song': song}, kwargs)

    @Tool('send_qq_image', platforms='qq', description='把本地图片真正发送给当前私聊对象；取得图片路径不代表已发送。', parameters=[parameter('image_path', '本地图片完整路径')])
    async def image(self, image_path='', **kwargs):
        try:
            path = Path(image_path).resolve()
            if path.is_relative_to(DIFFUSION_OUTPUT):
                raise ValueError('本地生成的图片由生图任务发回发起会话')
            if path.is_relative_to(TOOLS/'pixiv-downloads'):
                source, streams = await self.social_manager().source(kwargs)
                await asyncio.to_thread(validate_pixiv_image, image_path, source['session_id'], str(source['user_id']))
        except (ValueError, OSError, KeyError) as error:
            return {'success': False, 'sent': False, 'error': str(error)}
        return await self._invoke_media('send_qq_image', {'image_path': image_path}, kwargs)

    @Tool(
        'generate_qq_image',
        platforms='qq',
        description='用户明确要画新图时用本机 Stable Diffusion 生成一张并自动发回当前私聊，仅主人可用。pending 表示正在画，不要重复调用或另用发图工具。搜现有图片继续用 Pixiv。',
        parameters=[parameter(
            'prompt',
            '简短英文描述主体、动作、构图和画风。小鲸鱼是鲸鱼。画大肥鱼时只描述动作、表情和环境，外观与二次元画风由工具注入；只有用户点名才另加 Q 版画风。',
        ), parameter('kind', '普通图省略；画大肥鱼本人用 expression 表情特写或 scene 日常场景；只画她的环境用 environment。', False),
            parameter('reference', '可选，大肥鱼表情库里的准确名称，例如小问号、害羞、哈欠、读好书。表情特写默认参考小问号，日常图默认用角色设定。', False)],
    )
    async def draw_image(self, prompt='', kind='', reference='', **context):
        return await self._queue_drawing(prompt, kind or ('scene' if reference else None), context,
                                         spontaneous=False, reference=reference)

    @Tool(
        'share_feiyu_scene',
        platforms='qq',
        description='聊天时给主人主动配一张大肥鱼手绘同人图，限当前情绪或日常场景。适合普通表情包表达不出的具体瞬间。图会自动发回原会话，继续正常聊天。用户点名画图用generate_qq_image。',
        parameters=[parameter('kind', 'status 画自己刚回答的当前状态，expression 表情特写，scene 日常场景，environment 环境空镜。'),
                    parameter('detail', '只写当前一幕的简短英文可见细节，例如表情、正在做的动作、光线和背景。画近况时必须和刚发的回答一致。角色外观和手绘二次元画风由工具注入，请勿重复指定发色、服饰、chibi 或全图蓝色。'),
                    parameter('state_reply', 'kind=status 时必填，逐字引用刚发出去的状态短句或片段，至少四个字。', False),
                    parameter('reference', '只在需要还原某个表情时选库里的准确名称，例如害羞、哈欠。表情特写默认小问号，日常和近况图通常省略。', False)],
    )
    async def share_feiyu_scene(self, kind='', detail='', state_reply='', reference='', **context):
        if kind not in FEIYU_FRAMING:
            return {'status': 'skipped', 'sent': False, 'note': '缺少有效场景类别'}
        return await self._queue_drawing(detail, kind, context, spontaneous=True,
                                         state_reply=state_reply, reference=reference)

    async def _queue_drawing(self, prompt, kind, context, *, spontaneous, state_reply='', reference=''):
        try:
            source, _ = await self.social_manager().source(context)
        except ValueError as error:
            return {'status': 'unavailable', 'sent': False, 'note': str(error)}
        if str(source['user_id']) != OWNER:
            return {'status': 'unavailable', 'sent': False, 'note': '本地画图目前仅供主人试用'}
        if kind is not None and kind not in FEIYU_FRAMING:
            return {'status': 'unavailable', 'sent': False, 'note': '画面类别无效'}
        if kind == 'environment' and reference:
            return {'status': 'unavailable', 'sent': False, 'note': '环境空镜无需人物参考；画自己请选日常场景'}
        prompt = prompt.strip()
        if not prompt or len(prompt) > 600 or '\x00' in prompt:
            return {'status': 'unavailable', 'sent': False, 'note': '需要 1 至 600 字符的绘图描述'}
        message_id = None
        requirements = {'mode': 'spontaneous' if spontaneous else 'requested', 'kind': kind, 'drawing': prompt}
        try:
            messages = await self.ctx.message.get_recent(source['session_id'], limit=20)
            incoming = latest_incoming(messages, OWNER)
            age = time.time() - float(incoming['timestamp'])
            status_question = 0 <= age <= 180 and is_status_question(incoming.get('processed_plain_text') or '')
            if 0 <= age <= 180:
                requirements['user_message'] = str(incoming.get('processed_plain_text') or '')[:350]
            if status_question and (not spontaneous or kind != 'status'):
                return {'status': 'needs_status_reply', 'sent': False, 'note': '对方是在问近况。先正常回答；然后可用 share_feiyu_scene 的 status 模式画刚刚说的状态。'}
            if spontaneous:
                if not 0 <= age <= 180:
                    return {'status': 'skipped', 'sent': False, 'note': '当前没有新鲜的私聊话题'}
                message_id = str(incoming['message_id'])
                if kind == 'status' and (len(state_reply.strip()) < 4 or not has_current_reply(messages, incoming, state_reply.strip())):
                    return {'status': 'needs_status_reply', 'sent': False, 'note': '先发出当下的文字状态，再引用原话片段配图。'}
                if kind == 'status':
                    requirements['described_state'] = state_reply.strip()[:200]
        except (ValueError, TypeError, KeyError):
            if spontaneous:
                return {'status': 'skipped', 'sent': False, 'note': '无法确认本轮私聊话题'}
        sticker = None
        if reference or kind == 'expression':
            try:
                sticker = await asyncio.to_thread(resolve_sticker, reference or DEFAULT_STICKER)
            except ValueError as error:
                return {'status': 'unavailable', 'sent': False, 'note': str(error)}
        if self._drawing_task and not self._drawing_task.done():
            return {'status': 'busy', 'sent': False, 'note': '已有一张图在画，完成后会自动发回'}
        now = time.monotonic()
        if not spontaneous and self._drawing_started is not None and now - self._drawing_started < 60:
            return {'status': 'busy', 'sent': False, 'note': '刚刚已提交过画图，请稍后再试'}
        if spontaneous:
            if message_id == self._scene_message:
                return {'status': 'skipped', 'sent': False, 'note': '这条消息已提交过配图，继续聊天就好'}
            self._scene_message = message_id
        self._drawing_started = now
        self._drawing_task = asyncio.create_task(self._draw_and_send(prompt, kind, context, message_id, sticker, requirements))
        return {'status': 'pending', 'sent': False, 'note': '完成后会自动发回当前私聊'}

    async def _latest_owner_message(self, session_id):
        messages = await self.ctx.message.get_recent(session_id, limit=20)
        return latest_incoming(messages, OWNER)

    async def _draw_and_send(self, prompt, kind, context, message_id, sticker, requirements):
        note = '这张没画出来，等会儿再试吧。'
        key = uuid.uuid4().hex
        try:
            image_prompt = prompt
            note = '我没确认这张符合刚才说的内容，先不发了。'
            models = await asyncio.to_thread(backend.read_model_config, TOOLS.parent)
            for attempt in range(2):
                if attempt and message_id is not None and str((await self._latest_owner_message(context['stream_id']))['message_id']) != message_id:
                    await self._record_quality('image', key, '话题已变化')
                    return
                path = await generate_image(image_prompt, secrets.randbelow(2**31), kind, sticker)
                if message_id is not None and str((await self._latest_owner_message(context['stream_id']))['message_id']) != message_id:
                    await self._record_quality('image', key, '话题已变化')
                    return
                review = await review_image(path, requirements, models)
                decision = review.decision
                if decision == 'pass':
                    break
                if attempt == 0 and decision in IMAGE_REVISIONS:
                    image_prompt = review.revised_prompt or f'{IMAGE_REVISIONS[decision]}, {prompt}'
                    self.ctx.logger.info('生图自检发现%s不符合，重画一次', decision)
                else:
                    await self._record_quality('image', key, '验收未通过')
                    if message_id is None:
                        await self.ctx.send.text(note, context['stream_id'], storage_message=True, sync_to_maisaka_history=True)
                    return
            if message_id is not None and str((await self._latest_owner_message(context['stream_id']))['message_id']) != message_id:
                await self._record_quality('image', key, '话题已变化')
                return
            note = '图画好了，不过 QQ 没有确认收到。'
            receipt = await self._execute_media('send_qq_image', {'image_path': str(path)}, context)
            if receipt.get('sent') and receipt.get('message_id'):
                await self._record_quality('image', key, '修正后发送' if attempt else '一次通过并发送')
                self.ctx.logger.info(f"本地生图发送成功 message_id={receipt['message_id']}")
                return
            await self._record_quality('image', key, '投递未确认')
            self.ctx.logger.warning('本地生图未取得发送回执')
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, httpx.HTTPError) as error:
            self.ctx.logger.warning('本地生图或验收失败: %s', type(error).__name__)
            await self._record_quality('image', key, '生成或验收异常')
            if '语音模型服务正在运行' in str(error):
                note = '语音模型还在占用显卡，先暂停它，我就能画啦。'
        if message_id is not None:
            return
        try:
            await self.ctx.send.text(note, context['stream_id'], storage_message=True, sync_to_maisaka_history=True)
        except Exception as error:
            self.ctx.logger.warning(f'本地生图状态未能发回: {error}')

    @Tool('send_qq_sticker', platforms='qq', description='首选的大肥鱼扩展表情工具：按情绪或名称检索并真正发回当前私聊。闲聊适合接梗、开心、害羞、困或无语时可主动发一张，不必等用户要求；优先于原生图库，不要刷屏。', parameters=[parameter('name', '表情名称或情绪关键词')])
    async def sticker(self, name='', **kwargs):
        return await self._invoke_media('send_qq_sticker', {'name': name}, kwargs)


def create_plugin():
    return FeiyuMediaPlugin()
