"""将真实 QQ 来信绑定到有次数和预算限制的视觉找图任务。"""
from pathlib import Path
import asyncio
import hashlib
import json
import subprocess
import sys
import time

from maibot_sdk import Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from .proactive import BOT

ROOT = Path(__file__).resolve().parents[2] / 'feiyu-tools'
PIXIV = ROOT / 'pixiv-downloads'
WORKER = ROOT / 'pixiv-integration/vision_search.py'
PYTHON = ROOT / 'pixiv-mcp-server/.venv/Scripts/python.exe'
INSTRUCTION = ('【找图】用户要按描述找插画时，调用find_pixiv_images，把完整条件保留在requirements里。'
               '它会每轮看3至5张，最多两轮；stop_search=true后不再换词、翻页或改用浏览器反复搜。'
               '只把返回images里的路径交给send_qq_image。stopped/limited表示没完成或受限，不能说搜过但图不合适；'
               '只有images_checked大于0才说看过候选。没合格图就自然说明，不能凑图或声称看过没看的图片。')


def validate_pixiv_image(image_path, session, user_id):
    path = Path(image_path).resolve()
    if not path.is_relative_to(PIXIV):
        return
    receipt = path.parent / 'approval.json'
    if not receipt.is_file():
        raise ValueError('这张Pixiv图片还没有通过视觉筛选，请先用find_pixiv_images找图。')
    approval = json.loads(receipt.read_text(encoding='utf-8'))
    if approval['session_id'] != session or str(approval['user_id']) != str(user_id):
        raise ValueError('这张图属于另一个私聊的找图任务，不能串发。')
    if time.time() - approval['created_at'] > 86400:
        raise ValueError('这张图的筛选结果已过期，请重新确认。')
    expected = approval['images'].get(str(path))
    if not expected or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError('图片不在合格名单中，或图片内容已经变化，不能发送。')


def latest_incoming(messages, user_id):
    if not isinstance(messages, list):
        raise ValueError('无法读取真实来信，本次不启动付费找图。')
    matches = [row for row in messages if str(row.get('message_info', {}).get('user_info', {}).get('user_id', row.get('user_id', ''))) == user_id and row.get('message_id') and not row.get('is_notify')]
    if not matches:
        raise ValueError('没有找到这次私聊的真实用户来信。')
    def stamp(row):
        # SDK 的 MessageDict.timestamp 是 Unix 秒数字符串，不是 ISO 日期。
        return float(row['timestamp'])
    return max(matches, key=stamp)


async def run_search_worker(request):
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    process = await asyncio.create_subprocess_exec(str(PYTHON), '-X', 'utf8', str(WORKER), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **options)
    try:
        output, error = await asyncio.wait_for(process.communicate(json.dumps(request, ensure_ascii=False).encode('utf-8')), timeout=55)
    except (TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise RuntimeError('视觉找图任务未正常完成')
    return json.loads(output)


class PixivMixin:
    async def diagnose_pixiv_preflight(self):
        streams = await self.ctx.chat.get_private_streams(platform='qq')
        report = []
        for stream in streams:
            if str(stream.get('account_id')) != BOT or stream.get('group_id'):
                continue
            messages = await self.ctx.message.get_recent(stream['session_id'], limit=20)
            if not messages:
                report.append({'session_id': stream['session_id'], 'user_id': str(stream['user_id']), 'recent_messages': 0})
                continue
            incoming = latest_incoming(messages, str(stream['user_id']))
            report.append({'session_id': stream['session_id'], 'user_id': str(stream['user_id']), 'message_id': str(incoming['message_id']), 'timestamp_type': type(incoming['timestamp']).__name__, 'timestamp_seconds': float(incoming['timestamp'])})
        path = Path(self.ctx.paths.data_dir) / 'pixiv-preflight-check.json'
        await asyncio.to_thread(path.write_text, json.dumps({'checks': report, 'send_attempted': False, 'vision_calls': 0}, ensure_ascii=False, indent=2), encoding='utf-8')
        checked = sum('timestamp_seconds' in item for item in report)
        return f'视觉找图预检通过：已读取{len(report)}个真实私聊，其中{checked}个有近期来信且数字时间戳正常；其余暂无近期消息。没有发消息或调用视觉模型。'

    @Tool('find_pixiv_images', platforms='qq', chat_scope='private', description='按完整画面要求在Pixiv找图并调用DeepSeek视觉筛选。每轮3至5张，最多两轮，找到合格图即停止。返回stop_search后不得换词、改浏览器找同一请求。只发送返回images中的image_path，附画师和作品链接；无合格图自然说明，不凑数。', parameters=[
        ToolParameterInfo(name='requirements', param_type=ToolParamType.STRING, description='完整画面要求，保留人物、颜色、姿势、构图等条件，1至600字', required=True),
        ToolParameterInfo(name='keywords', param_type=ToolParamType.ARRAY, items_schema={'type': 'string'}, description='最多两个保持相同主题的搜索词；第二个只在第一轮无合格图时使用', required=True),
        ToolParameterInfo(name='count', param_type=ToolParamType.INTEGER, description='希望发送的合格图数，1至3，默认1；不为凑数量继续搜索', required=False),
    ])
    async def find_pixiv_images(self, requirements='', keywords=None, count=1, **context):
        try:
            source, streams = await self.social_manager().source(context)
            user_id = str(source['user_id'])
            messages = await self.ctx.message.get_recent(source['session_id'], limit=20)
            incoming = latest_incoming(messages, user_id)
            if not isinstance(keywords, list) or any(not isinstance(word, str) for word in keywords):
                raise ValueError('keywords必须是搜索词数组')
            if not isinstance(requirements, str) or not 1 <= len(requirements.strip()) <= 600:
                raise ValueError('请完整给出1至600字的画面条件')
            request = {'session_id': source['session_id'], 'user_id': user_id, 'message_id': str(incoming['message_id']), 'requirements': requirements.strip(), 'keywords': keywords[:2], 'count': max(1, min(3, int(count)))}
            result = await run_search_worker(request)
            return {'success': True, 'content': json.dumps(result, ensure_ascii=False), **result}
        except Exception as error:
            self.ctx.logger.exception('Pixiv视觉找图停止')
            result = {'status': 'stopped', 'stop_search': True, 'images': [], 'rounds': 0, 'images_checked': 0, 'vision_calls': 0, 'error_type': type(error).__name__, 'note': '找图在准备阶段未能完成，还没有检查候选图片。本条请求先停下，不再换关键词、换工具重试。'}
            # 停止是已处理的业务结果；不能标成 RPC 失败诱发 Planner 自动补救重试。
            return {'success': True, 'content': json.dumps(result, ensure_ascii=False), **result}
