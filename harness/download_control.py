"""Job ownership, cooperative controls and verified reconnecting acquisition."""
import contextlib
import hashlib
import http.client
import os
import re
import socket
import threading
import urllib.request
import uuid


class ControlError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class DownloadCancelled(RuntimeError):
    code = 'download_cancelled'


class DownloadError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


_LOCAL = threading.local()
_DESTINATIONS = {}
_DESTINATIONS_LOCK = threading.Lock()


def current():
    return getattr(_LOCAL, 'control', None)


@contextlib.contextmanager
def bind(control):
    previous = current()
    _LOCAL.control = control
    try:
        yield
    finally:
        _LOCAL.control = previous


class JobControl:
    def __init__(self, job_id=None):
        self.id = job_id or uuid.uuid4().hex
        self._condition = threading.Condition(threading.RLock())
        self._status = 'running'
        self._blocked = []
        self._generation = 0
        self._responses = set()

    def snapshot(self):
        with self._condition:
            available = self._status in ('running', 'paused') and not self._blocked
            return {'jobId': self.id, 'controlStatus': self._status,
                    'paused': self._status == 'paused',
                    'cancelRequested': self._status in ('cancelling', 'cancelled'),
                    'canPause': available and self._status == 'running',
                    'canResume': available and self._status == 'paused',
                    'canCancel': available,
                    'controlReason': self._blocked[-1] if self._blocked else
                                     '' if available else 'This job has finished or is stopping.'}

    def request(self, action):
        if action not in ('pause', 'resume', 'cancel'):
            raise ControlError('invalid_control', 'Choose pause, resume or cancel.')
        with self._condition:
            snapshot = self.snapshot()
            if not snapshot['can' + action.title()]:
                raise ControlError('control_unavailable', snapshot['controlReason'] or
                                   'This control is unavailable in the current job state.')
            self._status = {'pause': 'paused', 'resume': 'running', 'cancel': 'cancelling'}[action]
            if action != 'resume':
                self._generation += 1
            responses = list(self._responses) if action != 'resume' else []
            self._condition.notify_all()
        # Shutdown interrupts a blocked HTTP read without waiting for its buffer
        # lock. The worker closes the response and reopens after resume.
        for response in responses:
            try:
                response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
            except (AttributeError, OSError):
                pass
        return self.snapshot()

    def checkpoint(self):
        with self._condition:
            while self._status == 'paused':
                self._condition.wait()
            if self._status in ('cancelling', 'cancelled'):
                raise DownloadCancelled('Download cancelled. Verified completed files were kept.')
            if self._status == 'error':
                raise RuntimeError('Download job stopped after a transfer failure.')
            return self._generation

    def interrupted(self, generation):
        with self._condition:
            return self._generation != generation

    @contextlib.contextmanager
    def response(self, response):
        with self._condition:
            self._responses.add(response)
        try:
            yield
        finally:
            with self._condition:
                self._responses.discard(response)

    @contextlib.contextmanager
    def noninterruptible(self, reason):
        with self._condition:
            self.checkpoint()
            self._blocked.append(reason)
        try:
            yield
        finally:
            with self._condition:
                self._blocked.pop()

    def finish(self, status='done'):
        with self._condition:
            self._status = 'cancelled' if self._status in ('cancelling', 'cancelled') else status
            if self._status in ('error', 'cancelled'):
                self._generation += 1
                responses = list(self._responses)
            else:
                responses = []
            self._condition.notify_all()
        for response in responses:
            try:
                response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
            except (AttributeError, OSError):
                pass


def _verified(path, size, hashes):
    if not os.path.isfile(path) or (size is not None and os.path.getsize(path) != size):
        return False
    if not hashes:
        return size is not None
    digests = {name: hashlib.new(name) for name in hashes}
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1 << 20), b''):
            for digest in digests.values():
                digest.update(block)
    return all(digests[name].hexdigest() == value.lower() for name, value in hashes.items())


def _open(opener, request, timeout, control, generation):
    """Let an owned worker leave a pending connect/header request on cancel.

    The opener thread owns no files or job callbacks. A late response is closed
    by that thread, so cancelling never leaves another writer behind.
    """
    ready, guard, result = threading.Event(), threading.Lock(), {}

    def connect():
        response = None
        try:
            response = opener(request, timeout=timeout)
            with guard:
                if result.get('abandoned') or control.interrupted(generation):
                    response.close()
                    result['retry'] = True
                else:
                    result['response'] = response
        except Exception as error:
            with guard:
                result['error'] = error
        finally:
            ready.set()
    threading.Thread(target=connect, name='download-connect', daemon=True).start()
    try:
        while not ready.wait(.05):
            control.checkpoint()
        control.checkpoint()
        with guard:
            if 'error' in result:
                raise result['error']
            return result.pop('response', None)
    except Exception:
        with guard:
            result['abandoned'] = True
            response = result.pop('response', None)
            if response:
                response.close()
        raise


def download(url, destination, *, control=None, **options):
    """Serialize writers to a shared verified destination across owned jobs."""
    control = control or current() or JobControl()
    key = os.path.normcase(os.path.abspath(os.fspath(destination)))
    with _DESTINATIONS_LOCK:
        lock = _DESTINATIONS.setdefault(key, threading.Lock())
    while not lock.acquire(timeout=.05):
        control.checkpoint()
    try:
        return _download(url, destination, control=control, **options)
    finally:
        lock.release()


def _download(url, destination, *, size=None, hashes=None, control=None,
             progress=None, opener=None, timeout=30, headers=None,
             validate_response=None, max_bytes=None):
    """Verify before replacing; paused connections resume or safely restart.

    A partial belongs only to this invocation. Completed destinations are reused
    after verifying their size and hashes; partials are removed on every failure.
    """
    control = control or current() or JobControl()
    hashes, progress = hashes or {}, progress or (lambda _: None)
    destination = os.fspath(destination)
    os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
    control.checkpoint()
    if _verified(destination, size, hashes):
        progress(os.path.getsize(destination))
        return destination
    temporary = destination + '.part'
    opener = opener or urllib.request.urlopen
    offset, validator = 0, None
    try:
        # Never inherit unverified bytes from another transaction.
        with open(temporary, 'wb'):
            pass
        while True:
            generation = control.checkpoint()
            if offset and _verified(temporary, size, hashes):
                break
            request_headers = {'User-Agent': 'minecraft-launcher-harness/1.0',
                               'Accept-Encoding': 'identity', **(headers or {})}
            if offset:
                request_headers['Range'] = 'bytes=%d-' % offset
                if validator:
                    request_headers['If-Range'] = validator
            request = urllib.request.Request(url, headers=request_headers)
            try:
                response = _open(opener, request, timeout, control, generation)
                if response is None:
                    continue
                with response, control.response(response):
                    if validate_response:
                        validate_response(response)
                    if control.interrupted(generation):
                        continue
                    status = getattr(response, 'status', None)
                    if status is None:
                        status = response.getcode() if hasattr(response, 'getcode') else 200
                    response_headers = getattr(response, 'headers', {})
                    if offset and status == 206:
                        match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response_headers.get('Content-Range', ''))
                        if (not match or int(match[1]) != offset or int(match[2]) < offset or
                                (size is not None and int(match[3]) != size)):
                            raise RuntimeError('Server returned an invalid resume range.')
                    elif status == 200 or status is None:
                        offset = 0
                        progress(0)
                    else:
                        raise RuntimeError('Server could not resume this download safely.')
                    validator = response_headers.get('ETag') or response_headers.get('Last-Modified') or validator
                    declared = response_headers.get('Content-Length')
                    if declared is not None and size is not None and int(declared) != size - offset:
                        raise DownloadError('size_mismatch', 'Download size disagrees with selected metadata.')
                    with open(temporary, 'ab' if offset else 'wb') as staged:
                        read = response.read1 if isinstance(response, http.client.HTTPResponse) else response.read
                        while not control.interrupted(generation):
                            block = read(65536)
                            if not block:
                                break
                            if control.interrupted(generation):
                                break
                            staged.write(block)
                            offset += len(block)
                            if ((size is not None and offset > size) or
                                    (max_bytes is not None and offset > max_bytes)):
                                raise DownloadError('size_mismatch', 'Downloaded file exceeds its selected size.')
                            progress(offset)
                        staged.flush()
                        os.fsync(staged.fileno())
                if control.interrupted(generation):
                    continue
                break
            except Exception:
                if control.interrupted(generation):
                    continue
                raise
        control.checkpoint()
        if (size is not None or hashes) and not _verified(temporary, size, hashes):
            code = 'size_mismatch' if size is not None and os.path.getsize(temporary) != size else 'hash_mismatch'
            raise DownloadError(code, 'Downloaded file failed its size or verification hash check: ' + url)
        with control.noninterruptible('Finishing a verified file'):
            os.replace(temporary, destination)
        progress(offset)
        return destination
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
