"""Configured Chat Completions adapter; never reads evaluation labels."""
import json
import os
import time
from http.client import HTTPException
from pathlib import Path
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from app.prompts import SYSTEM_PROMPT
from app.schema import CATEGORIES, validate_prediction

PROMPT_VERSION = 'facts-v1'
CONTRACT = '''返回一个 JSON 对象，字段如下：
verdict: hallucination / no_hallucination / not_verifiable；
types: 分类 key 数组；severity: low / medium / high 或 null；reason: 简短中文理由；
claims: 逐条事实数组，每项含 relation (supported/contradicted/unsupported/not_verifiable)、reply_quote (回复中的连续原文)、knowledge_quote (知识中的连续原文，无依据时可为空)、reason (中文说明)。
幻觉必须至少有一个 contradicted 或 unsupported 声明、分类和严重程度。
正常判断不能包含冲突或无依据声明，types 为 []，severity 为 null。
contradicted 必须提供非空知识引用。不要合并不连续原文，不要在引用中添加省略号。
不需要返回案例 ID。只输出 JSON，不使用 Markdown。'''
PROMPT = SYSTEM_PROMPT + '\n分类：' + json.dumps(CATEGORIES, ensure_ascii=False) + '\n' + CONTRACT

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def config():
    values = {}
    path = Path(__file__).resolve().parent.parent / '.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip().strip('\"\'')
    for key in ('LLM_API_KEY', 'LLM_BASE_URL', 'LLM_MODEL'):
        values[key] = os.environ.get(key) or values.get(key, '')
        if not values[key]:
            raise ValueError(f'缺少配置 {key}')
    url = urlsplit(values['LLM_BASE_URL'])
    if url.scheme != 'https' or not url.hostname or url.username or url.query or url.fragment:
        raise ValueError('LLM_BASE_URL 必须为不含凭据和查询参数的 HTTPS 地址')
    return values


class LLMDetector:
    def __init__(self):
        self.settings = config()
        self.model = self.settings['LLM_MODEL']
        base = self.settings['LLM_BASE_URL'].rstrip('/')
        self.endpoint = base + ('/v1' if not urlsplit(base).path else '') + '/chat/completions'
        self.last_response = None
        self.opener = build_opener(NoRedirect())

    def detect(self, row):
        payload = {'model': self.model, 'temperature': 0, 'stream': False,
                   'messages': [{'role': 'system', 'content': PROMPT},
                                {'role': 'user', 'content': json.dumps({k:row[k] for k in ('user_question','system_reply','knowledge_base')}, ensure_ascii=False)}]}
        self.last_response = None
        for attempt in range(2):
            request = Request(self.endpoint, data=json.dumps(payload).encode(), headers={
                'Authorization': 'Bearer ' + self.settings['LLM_API_KEY'], 'Content-Type': 'application/json', 'User-Agent':'kefu-check/0.1'})
            started = time.monotonic()
            try:
                with self.opener.open(request, timeout=120) as response:
                    raw = response.read().decode('utf-8')
                raw = raw.replace(self.settings['LLM_API_KEY'], '[REDACTED]')
                body = json.loads(raw)
                self.last_response = {'response': body, 'elapsed_seconds': round(time.monotonic()-started,3), 'attempt':attempt+1}
                text = body['choices'][0]['message']['content'].strip()
                if text.startswith('```'):
                    text = '\n'.join(text.splitlines()[1:-1])
                result = json.loads(text)
                if not isinstance(result, dict): raise ValueError('LLM 输出必须为对象')
                result['id'] = row['id']
                return validate_prediction(result, row)
            except HTTPError as exc:
                if attempt == 0 and exc.code in (429,500,502,503,504):
                    time.sleep(2); continue
                raise ValueError(f'LLM HTTP {exc.code}，未记录响应正文以避免泄露配置') from None
            except (URLError, TimeoutError, HTTPException, ConnectionError):
                if attempt == 0:
                    time.sleep(2); continue
                raise ValueError('LLM 网络连接失败或超时') from None
            except (KeyError, TypeError, AttributeError, json.JSONDecodeError):
                raise ValueError('LLM 响应格式不合法，请检查本次 raw 记录') from None
