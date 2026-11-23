"""QQ 好友候选的排序、展示和选择。"""
import re
import unicodedata

from pypinyin import lazy_pinyin
from rapidfuzz.fuzz import ratio


def normalized(value):
    return ''.join(unicodedata.normalize('NFKC', str(value)).casefold().split())


def match_contacts(people, target):
    query = normalized(target).lstrip('@')
    if not query:
        return None, []
    if query in people:
        return people[query], []
    exact, ranked = [], []
    phonetic = lazy_pinyin(query)
    for uid, person in people.items():
        names = {normalized(value) for value in (person.get('remark'), person.get('nickname'), *person.get('aliases', [])) if value}
        if query in names:
            exact.append(person)
            continue
        score = 0
        if query.isdecimal():
            if len(query) >= 3 and query in uid:
                score = 95 if uid.startswith(query) or uid.endswith(query) else 85
        else:
            for name in names:
                if query in name:
                    score = max(score, 92 + 7 * len(query) / len(name))
                if len(query) >= 2:
                    score = max(score, ratio(query, name))
                    if re.search(r'[\u3400-\u9fff]', name):
                        syllables = lazy_pinyin(name)
                        if phonetic == syllables or query == ''.join(syllables):
                            score = max(score, 96)
                        elif len(phonetic) >= 3:
                            score = max(score, .9 * ratio(phonetic, syllables))
        if score >= 60:
            ranked.append((-score, uid, person))
    if len(exact) == 1:
        return exact[0], []
    return None, sorted(exact, key=lambda p: p['user_id']) if exact else [row[2] for row in sorted(ranked)[:5]]


def menu_text(people):
    lines = ['你说的是哪位好友？']
    tails = [p['user_id'][-4:] for p in people]
    for i, p in enumerate(people, 1):
        names = list(dict.fromkeys(' '.join(str(p.get(k) or '').split()) for k in ('remark', 'nickname')))
        label = ' / '.join(filter(None, names))
        tail = p['user_id'][-4:]
        number = 'QQ 尾号 ' + tail if tails.count(tail) == 1 else 'QQ ' + p['user_id']
        lines.append(f'{i}. {label}（{number}）')
    return '\n'.join([*lines, '回序号就行，也可以回“都不是”。'])


def choice_index(text):
    text = normalized(text).strip('。！!～~')
    if text in ('取消', '算了', '都不是', '都不对'):
        return 0
    match = re.fullmatch(r'(?:我选|就选|选|就|是)?第?(\d{1,2}|[一二两三四五])(?:个|位|号)?', text)
    if not match:
        return None
    value = match[1]
    if value.isdecimal():
        return int(value) or -1
    return {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5}[value]


def parse_menu_reply(text, count):
    """整句必须是明确选择，可附带一首要发给同一收件人的歌。"""
    text = unicodedata.normalize('NFKC', text).strip().rstrip('。！!～~')
    addition = re.fullmatch(
        r'(.+?)[,、。\s]*(?:然后|顺便)?(?:再)?把(?:上面|之前|前面|刚才)?(?:的|那首)?'
        r'(?:《([^《》]+)》|([^,，。！？!?《》]{1,80}?))(?:也|再|一起|一并)(?:转发|发|转)给(?:他|她|对方)(?:一份)?(?:吧)?', text)
    head = addition[1].rstrip(',、。 ').strip() if addition else text
    index = choice_index(head)
    if index is None and re.fullmatch(r'(?:对[,， ]*|嗯[,， ]*)?(?:就?是(?:这个人|这位|他|她)|就这(?:个|位)|没错|对的|对)', head):
        index = 1 if count == 1 else -1
    song = (addition[2] or addition[3]).strip() if addition and index and index > 0 else ''
    return index, song
