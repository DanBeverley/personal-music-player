"""Bounded public song research. Private conversation text never leaves for search."""
from collections import OrderedDict
from html.parser import HTMLParser
import json
from ipaddress import ip_address
import re
import socket
from threading import Lock
import time
from urllib.parse import urljoin, urlsplit

import requests
import urllib3

_cache = OrderedDict()
_lock = Lock()


def _passages(parts, topic):
    terms = set(topic.split())
    if topic in {'composition','recording history','release date'}:
        terms.update({'written','writing','composed','developed','recorded','released','began'})
    def score(part):
        lowered = part.casefold()
        return sum(term in lowered for term in terms) + bool(re.search(r'\b(?:19|20)\d{2}\b', part))
    selected = sorted(enumerate(parts), key=lambda row:(-score(row[1]),row[0]))[:7]
    return '\n'.join(part for _,part in sorted(selected))[:5200]


def _reference_song(title, artist, topic):
    """One fixed-host reference API, so thin web snippets are not the whole corpus."""
    params={'action':'query','generator':'search','gsrsearch':f'intitle:"{title}" {artist}',
            'gsrnamespace':0,'gsrlimit':1,'prop':'extracts','explaintext':1,'format':'json'}
    deadline = time.monotonic()+3
    with requests.get('https://en.wikipedia.org/w/api.php',params=params,
            headers={'User-Agent':'Neatie-Song-Context/1.0'},
            timeout=(1.5,2),allow_redirects=False,stream=True) as response:
        if response.status_code != 200:
            return None
        body = bytearray()
        for block in response.iter_content(16384):
            body.extend(block)
            if len(body)>262144 or time.monotonic()>deadline:
                return None
        pages = json.loads(body).get('query',{}).get('pages',{})
    for page in pages.values():
        text = str(page.get('extract') or '')
        page_title = str(page.get('title') or '')
        if (artist.casefold() not in text.casefold()
                or title.casefold() not in page_title.casefold()):
            continue
        excerpt = _passages([part.strip() for part in text.split('\n') if len(part.strip())>45],topic)
        if excerpt:
            from urllib.parse import quote
            return {'title':page_title+' — Wikipedia',
                    'url':'https://en.wikipedia.org/wiki/'+quote(page_title.replace(' ','_')),
                    'excerpt':excerpt,'kind':'page_excerpt'}
    return None


def question_topic(question, history=()):
    text = str(question).casefold()
    if re.fullmatch(r'\s*(and )?(when|what year|how old)[? .]*', text):
        for turn in reversed(history):
            if turn.get('role') == 'user':
                topic = question_topic(turn.get('content', ''))
                if topic in {'composition', 'release date', 'recording history'}:
                    return topic
                break
    if re.search(r'\b(release|anniversary)\w*', text):
        return 'release date'
    if re.search(r'\b(recorded|recording)\b', text):
        return 'recording history'
    if re.search(r'\b(when|date|year|written|writing|compos)\w*', text):
        return 'composition'
    if re.search(r'\b(produc|studio|instrument|guitar|arrang|sound)\w*', text):
        return 'recording production musical arrangement'
    if re.search(r'\b(who|inspir|biograph|interview|real life|about whom)\w*', text):
        return 'song inspiration songwriter interview background'
    return 'song meaning themes songwriter interview'


class _Article(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip = 0
        self.capture = 0
        self.parts = []
        self.current = []

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'nav', 'footer', 'header', 'noscript'}:
            self.skip += 1
        if tag in {'p', 'h1', 'h2', 'li', 'dd', 'td'}:
            self.capture += 1

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'nav', 'footer', 'header', 'noscript'}:
            self.skip = max(0, self.skip-1)
        if tag in {'p', 'h1', 'h2', 'li', 'dd', 'td'}:
            self.capture = max(0, self.capture-1)
            if not self.capture:
                text = re.sub(r'\s+', ' ', ''.join(self.current)).strip()
                if len(text) >= 45:
                    self.parts.append(text)
                self.current = []

    def handle_data(self, data):
        if self.capture and not self.skip:
            self.current.append(data)


def _public_target(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
            or parsed.password or parsed.port not in (None, 443)):
        raise ValueError('non_public_context_url')
    addresses = {row[4][0] for row in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)}
    if not addresses or any(not ip_address(address).is_global for address in addresses):
        raise ValueError('non_public_context_address')
    return parsed, sorted(addresses)[0]


def _read_article(url, topic, deadline):
    """Pin each checked IP while preserving TLS hostname verification and SNI."""
    for _ in range(3):
        if time.monotonic() >= deadline:
            return ''
        parsed, address = _public_target(url)
        pool = urllib3.HTTPSConnectionPool(address, port=443,
            assert_hostname=parsed.hostname, server_hostname=parsed.hostname,
            cert_reqs='CERT_REQUIRED', ca_certs=requests.certs.where(), maxsize=1)
        response = None
        try:
            remaining = max(.2, min(2, deadline-time.monotonic()))
            response = pool.urlopen('GET', parsed.path+('?' + parsed.query if parsed.query else ''),
                headers={'Host':parsed.hostname, 'User-Agent':'Neatie-Song-Context/1.0',
                         'Accept':'text/html,text/plain'}, redirect=False, retries=False,
                preload_content=False, timeout=urllib3.Timeout(connect=remaining, read=remaining))
            if response.status in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers.get('Location', ''))
                continue
            if response.status != 200 or not any(kind in response.headers.get('Content-Type', '')
                                               for kind in ('text/html', 'text/plain')):
                return ''
            body = bytearray()
            while len(body) <= 524288 and time.monotonic() < deadline:
                block = response.read(min(16384, 524289-len(body)), decode_content=True)
                if not block:
                    break
                body.extend(block)
            parser = _Article()
            parser.feed(body[:524288].decode('utf-8', errors='replace'))
            return _passages(parser.parts,topic)
        finally:
            if response is not None:
                response.close()
            pool.close()
    return ''


def song_context(title, artist, question='', history=(), *, research=True):
    """Reuse context by song/topic, not private question or account identity."""
    from langgraph_assistant import _duckduckgo_search
    if not title or not artist:
        return []
    topic = question_topic(question, history)
    # Exact public identities stop general artist biographies outranking the song.
    clean = lambda value: re.sub(r'["\r\n]', ' ', value[:180]).strip()
    query = f'{clean(artist)} "{clean(title)}" {topic}'
    key = query.casefold()
    now = time.time()
    with _lock:
        for old in list(_cache):
            if _cache[old][0] <= now:
                _cache.pop(old)
        if research and key in _cache:
            _cache.move_to_end(key)
            return [dict(item) for item in _cache[key][1]]
    sources, seen = [], set()
    if research:
        try:
            reference = _reference_song(clean(title),clean(artist),topic)
            if reference:
                sources.append({**reference,'id':1})
                seen.add(reference['url'])
        except Exception:
            pass
    # A reference article can answer ordinary chronology directly; no second
    # search/provider round trip is required just to repeat the same facts.
    reference_terms = {
        'composition': r'\b(wrote|written|composed|began writing)\b',
        'recording history': r'\b(recorded|recording sessions)\b',
        'release date': r'\b(released|issued)\b',
    }
    complete_reference = (bool(sources) and topic in reference_terms
        and re.search(reference_terms[topic], sources[0]['excerpt'], re.IGNORECASE)
        and re.search(r'\b(?:19|20)\d{2}\b', sources[0]['excerpt']))
    values = []
    if not complete_reference:
        try:
            values = _duckduckgo_search(query, 5, cache_only=not research)
        except Exception:
            pass  # Keep a verified song reference if the secondary provider fails.
    for item in values[:5]:
        if len(sources)>=5:
            break
        url = str(item.get('href') or '')[:2000]
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme == 'https' and parsed.hostname and not parsed.username
                     and not parsed.password and parsed.port in (None, 443))
        except ValueError:
            valid = False
        if not valid or url in seen or not str(item.get('body') or '').strip():
            continue
        seen.add(url)
        sources.append({'id':len(sources)+1, 'title':str(item.get('title') or parsed.hostname)[:180],
                        'url':url, 'excerpt':str(item.get('body') or '')[:1200],
                        'kind':'search_excerpt'})
    deadline = time.monotonic()+5
    if research:
        # Prefer documents over navigation/discovery/search landing pages.
        words = set(re.findall(r'\w{3,}', title.casefold()))
        candidates = sorted((item for item in sources if item['kind']=='search_excerpt'), key=lambda item:(bool(re.search(
            r'/(?:discover|search|results|tag)/',urlsplit(item['url']).path)),
            -sum(word in item['title'].casefold() for word in words)))
        for item in candidates[:1 if any(s['kind']=='page_excerpt' for s in sources) else 2]:
            try:
                text = _read_article(item['url'], topic, deadline)
                if text:
                    item.update(excerpt=text, kind='page_excerpt')
            except Exception:
                pass  # A failed read remains an explicitly weaker search lead.
    if sources and research:
        with _lock:
            lifetime = 1800 if any(item['kind']=='page_excerpt' for item in sources) else 90
            _cache[key] = (time.time()+lifetime, sources)
            while len(_cache) > 64:
                _cache.popitem(last=False)
    return [dict(item) for item in sources]
