"""本机米画师集成 API 的 Host 与 Origin 防护。"""

from urllib.parse import urlsplit


_LOCAL_HOSTS = {'127.0.0.1', 'localhost', '::1'}


def is_local_request(request, require_origin=False):
    try:
        request_parts = urlsplit('//{}'.format(request.host))
        request_host = (request_parts.hostname or '').lower()
        request_port = request_parts.port
    except ValueError:
        return False
    if request_host not in _LOCAL_HOSTS:
        return False

    sources = []
    origin = request.headers.get('Origin', '').strip()
    referer = request.headers.get('Referer', '').strip()
    if origin:
        sources.append(origin)
    elif referer:
        sources.append(referer)
    elif require_origin:
        return False
    else:
        return True

    try:
        source = urlsplit(sources[0])
        source_host = (source.hostname or '').lower()
        source_port = source.port
    except ValueError:
        return False
    if source.scheme != request.scheme or source_host != request_host:
        return False
    return source_port == request_port
