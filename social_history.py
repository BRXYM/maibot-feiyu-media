"""Read only cross-conversation activity metadata, never private message text."""
from pathlib import Path
from contextlib import closing
from datetime import datetime, timezone, timedelta
import sqlite3

DATABASE_PATH = Path(__file__).resolve().parents[2] / 'data/MaiBot.db'
LOCAL_ZONE = timezone(timedelta(hours=8))


def read_activity(database_path, bot_id, period='today', *, at=None, include_day_context=True):
    path = Path(database_path)
    if not path.is_file():
        raise ValueError('聊天历史库不可用，不能断言没有其他人找过你')
    now = (at or datetime.now(LOCAL_ZONE)).astimezone(LOCAL_ZONE)
    until = now
    if period == 'today':
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == 'yesterday':
        until = now.replace(hour=0, minute=0, second=0, microsecond=0)
        since = until - timedelta(days=1)
    elif period == '24h':
        since = now - timedelta(hours=24)
    elif period == '7d':
        since = now - timedelta(days=7)
    elif period == 'all':
        since = None
    else:
        try:
            since = datetime.strptime(period, '%Y-%m-%d').replace(tzinfo=LOCAL_ZONE)
        except (ValueError, TypeError) as error:
            raise ValueError('period须为today、yesterday、24h、7d、all或YYYY-MM-DD日期') from error
        until = max(since, min(since + timedelta(days=1), now))
    start = since.replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%S') if since else '0001-01-01 00:00:00'
    end = until.replace(tzinfo=None).isoformat(sep=' ', timespec='microseconds' if until.microsecond else 'seconds')
    with closing(sqlite3.connect('file:' + path.resolve().as_posix() + '?mode=ro', uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        # Chat sessions survive process restarts; the SDK's active-stream list
        # only contains sessions restored into the current process.
        rows = db.execute('''
            SELECT s.session_id, s.user_id, s.user_nickname AS nickname,
                   s.user_cardname AS remark, s.account_id,
                   COUNT(m.id) AS incoming_messages,
                   MAX(COALESCE(m.received_at,m.timestamp)) AS last_incoming_at,
                   s.last_active_timestamp AS last_active_at
            FROM chat_sessions s
            LEFT JOIN mai_messages m ON m.session_id=s.session_id
                AND m.platform='qq' AND m.account_id=? AND m.user_id=s.user_id
                AND COALESCE(m.received_at,m.timestamp)>=?
                AND COALESCE(m.received_at,m.timestamp)<?
            WHERE s.platform='qq' AND s.account_id=?
                AND (s.group_id IS NULL OR s.group_id='')
            GROUP BY s.id
            ORDER BY last_incoming_at DESC, s.last_active_timestamp DESC
            LIMIT 100
        ''', (bot_id, start, end, bot_id)).fetchall()
    sessions = [dict(row) for row in rows]
    for row in sessions:
        row['user_id'] = str(row['user_id'])
        row['nickname'] = row['nickname'] or ''
        row['remark'] = row['remark'] or ''
        row['has_received_messages_in_period'] = row['incoming_messages'] > 0
    output = {'period': period, 'timezone': 'Asia/Shanghai', 'since': start if since else None,
            'until': end, 'queried_at': now.isoformat(),
            'calendar_date': since.date().isoformat() if since and period not in {'24h', '7d'} else None,
            'range_end_exclusive': True,
            'known_sessions': sessions,
            'recent_conversations': [s for s in sessions if s['incoming_messages'] > 0],
            'note': '这里统计实际收到的私聊消息；不把单独发送语音测试当成对方来聊天。不包含聊天正文。当前私聊安静多久不代表机器人全局离线。'}
    if period == 'today' and include_day_context:
        # 不把today偷偷改成“睡前一天”。并列日期证据，让模型按前文所指的日期解释。
        output['day_context'] = {}
        for label, window in [('previous_day', 'yesterday'), ('last24h', '24h')]:
            alternative = read_activity(path, bot_id, window, at=now, include_day_context=False)
            output['day_context'][label] = {k: alternative[k] for k in
                ['period', 'calendar_date', 'since', 'until', 'recent_conversations']}
        output['day_context']['interpretation'] = (
            'today仅统计上述自然日零点后，不代表用户正在谈论的整段一天。若前文仍谈零点前的下午、服务器故障等，'
            '需结合previous_day和last24h按实际日期回答；不要把“零点后只有一人”泛化成“刚过去的白天也只有一人”。'
            '需要时说明“零点后”和“刚才说的那一天”，不要求用户重复解释已明确的前文。')
    return output
