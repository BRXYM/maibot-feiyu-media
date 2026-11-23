"""根据 QQ 原消息和本地投递记录选择一次发送动作。"""
import asyncio
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


SONGS = Path(__file__).resolve().parents[2] / 'feiyu-tools/audio/singing-jobs'


def delivered_songs(owner, session):
    catalog = {}
    for path in SONGS.glob('*/delivery.json'):
        if not re.fullmatch(r'[0-9a-f]{32}', path.parent.name):
            continue
        try:
            receipt = json.loads(path.read_text('utf-8-sig'))
            if receipt.get('sent') is not True or receipt.get('message_type') != 'record' or str(receipt.get('target')) != owner:
                continue
            request = json.loads((path.parent / 'request.json').read_text('utf-8-sig'))
            if request.get('song') and request.get('route') == {'target': owner, 'session_id': session}:
                catalog.setdefault(request['song'], set()).add(int(receipt['message_id']))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return catalog


def named_song_request(text):
    text = text.strip().rstrip('。！!～~')
    prefix = r'(?:请|麻烦)?(?:你)?(?:帮我|替我)?'
    source = r'(?:你)?(?:(?:刚刚|刚才|之前|上次|刚)?唱(?:过|好|完)?的)?'
    match = re.fullmatch(prefix + r'(?:把|将)' + source +
                         r'(.+?)(?:这(?:两|几|些)首歌?)?(?:都|一起|也|再|一并)?(?:转发|分享|发|转)给\s*@?(.+?)(?:一份)?(?:吧)?', text)
    return (match[1].strip(), match[2].strip()) if match else None


def requested_titles(raw, catalog):
    # 先匹配完整曲名，“和”也可能属于歌名本身。
    titles = []
    remaining = raw.strip()
    while remaining:
        for title in sorted(catalog, key=len, reverse=True):
            match = re.match(r'(?:《' + re.escape(title) + r'》|' + re.escape(title) +
                             r')(?:\s*(?:以及|还有|和|与|及|、|，|,)\s*(?=.)|$)', remaining)
            if match:
                if title not in titles:
                    titles.append(title)
                remaining = remaining[match.end():]
                break
        else:
            raise ValueError(f'在这个私聊的已发语音里没找到“{remaining}”的对应录音，请核对歌名或引用原语音。')
    return titles


def delivered_song_ids(title, owner, session):
    return delivered_songs(owner, session).get(title, set())


def local_record(quote_id, owner, session):
    for receipt_path in SONGS.glob('*/delivery.json'):
        folder = receipt_path.parent
        if not re.fullmatch(r'[0-9a-f]{32}', folder.name):
            continue
        try:
            receipt = json.loads(receipt_path.read_text('utf-8-sig'))
            if (receipt.get('sent') is not True or str(receipt.get('message_id')) != str(quote_id)
                    or receipt.get('message_type') != 'record' or str(receipt.get('target')) != owner):
                continue
            request = json.loads((folder / 'request.json').read_text('utf-8-sig'))
            result = json.loads((folder / 'result.json').read_text('utf-8-sig'))
            if request.get('route') != {'target': owner, 'session_id': session} or result.get('status') != 'success':
                continue
            path = Path(result['audio_path']).resolve()
            if (path.parent != folder or path.name not in {'feiyu-full-song.mp3', 'feiyu-acapella.wav'}
                    or not 0 < path.stat().st_size <= 10 * 1024**2):
                continue
            if hashlib.sha256(path.read_bytes()).hexdigest() == result.get('audio_sha256'):
                return path.as_uri()
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def media_address(value):
    address = urlsplit(str(value or ''))
    if (address.scheme in ('https', 'http') and (address.hostname or '').endswith(('.qq.com', '.qq.com.cn', '.qpic.cn', '.gtimg.cn'))
            and not address.username and not address.password and address.port in (None, 80, 443)):
        return address.geturl()
    return None


async def select_delivery(api, original, uid, owner, session, bot):
    segments = original.get('message') or []
    kinds = [s.get('type') for s in segments]
    quote_id = int(original['message_id'])
    direct = ('直接转发', 'message.forward_friend_single_msg', {'user_id': int(uid), 'message_id': quote_id})
    if not segments:
        raise ValueError('QQ 返回的原消息没有可发送内容')
    if 'file' in kinds:
        if kinds != ['file']:
            raise ValueError('这条消息混合了附件和其他内容，目前无法完整重发')
        data = segments[0]['data']
        params = {'user_id': int(owner), 'file_id': data.get('file_id') or data.get('id')}
        if data.get('file_hash'):
            params['file_hash'] = data['file_hash']
        url = media_address(data.get('url'))
        if not url and params['file_id']:
            info = await api.call('adapter.snowluma.file.get_private_file_url', params=params)
            url = media_address(((info or {}).get('data') or {}).get('url'))
        if not url:
            raise ValueError('QQ 暂时没有提供这份附件的可用下载地址')
        return '上传原附件', 'file.upload_private_file', {'user_id': int(uid), 'file': url, 'name': str(data.get('name') or '')}
    if not set(kinds).intersection({'image', 'record', 'video'}):
        return direct
    outgoing = []
    method = '上传 QQ 原媒体'
    for segment in segments:
        kind, data = segment['type'], segment['data']
        if kind in ('text', 'face', 'at'):
            outgoing.append(segment)
            continue
        if kind not in ('record', 'image', 'video'):
            return direct
        source = None
        if kinds == ['record'] and str(original.get('sender', {}).get('user_id')) == bot:
            source = await asyncio.to_thread(local_record, quote_id, owner, session)
            if source:
                method = '上传本地唱歌源文件'
        source = source or media_address(data.get('url'))
        if not source and kind == 'record' and data.get('file'):
            info = await api.call('adapter.snowluma.file.get_record', file=data['file'], out_format='')
            source = media_address((info or {}).get('url'))
        if not source:
            if kind == 'record':
                raise ValueError('原语音已无法读取，QQ 和本地均无可用音源')
            return direct
        outgoing.append({'type': kind, 'data': {'file': source}})
    return method, 'message.send_private_msg', {'user_id': int(uid), 'message': outgoing}
