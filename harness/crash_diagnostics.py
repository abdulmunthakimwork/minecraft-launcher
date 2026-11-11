"""Local, read-only explanations of bounded Minecraft log evidence.

An error signature proves that an error was logged, not that it ended a game or
that a particular mod caused it. Session context controls that distinction.
"""
import math
import re


INPUT_LIMIT = 1024 * 1024
LINE_LIMIT = 10000
EVIDENCE_LIMIT = 4
EVIDENCE_TEXT_LIMIT = 360

_PREFIX = re.compile(r'^\s*(?:\[[^\]\r\n]*\]\s*)+:?\s*')
_ERROR = re.compile(r'(?:/(?:ERROR|FATAL)\]|\[(?:ERROR|FATAL)\])', re.I)
_EXCEPTION = re.compile(r'^(?:Caused by:\s*|Exception in thread "[^"\r\n]+"\s*)?(?:[\w$]+\.)*([\w$]+(?:Error|Exception))(?=\s*:|$)')
_START = re.compile(r'^\[harness\] launched Minecraft\b')
_FABRIC_START = re.compile(r'^Loading Minecraft [\w.+-]+ with (?:Fabric|Quilt) Loader\b')
_EXIT = re.compile(r'^\[harness\] game exited with code (-?\d+)\s*$')
_JAVA = re.compile(r'class file version (\d+)\.\d+.*up to (\d+)\.\d+', re.I)

_ADVICE = {
    'dependency': (
        'Missing or incompatible mod dependencies',
        'The mod loader reports missing or incompatible dependencies. Its named requirements are the best starting point.',
        ['Check the named dependency and required version in the evidence against Installed mods.',
         'Use releases for this installation’s Minecraft version and mod loader; review any dependency repair before applying it.'],
        'reported'),
    'duplicate-mods': (
        'Duplicate mods were reported',
        'The loader reports multiple files providing the same mod. This can prevent mod loading.',
        ['Compare the named duplicate files in Installed mods and keep the compatible version after reviewing them.',
         'Back up the installation before changing mod files.'],
        'reported'),
    'java-version': (
        'Java runtime is too old for a loaded class',
        'The runtime reports a class compiled for a newer Java version than it supports.',
        ['Check the installation’s Java executable and the Java version required by Minecraft and its mods.',
         'Choose a compatible Java runtime in General, or a compatible release of the named mod.'],
        'reported'),
    'memory': (
        'Java could not allocate memory',
        'Java reports a memory allocation failure. Heap limits, native memory pressure or a mod memory leak may be involved.',
        ['Check available system memory and close other heavy applications.',
         'Review the installation’s memory limit. Increase it only if system memory is available; excessive allocation can also fail.',
         'If the failure repeats, review recently changed mods and resource or shader packs.'],
        'reported'),
    'graphics': (
        'Graphics initialization failed',
        'The log reports an OpenGL or window graphics context failure. Driver support, the selected GPU or a graphics mod may be involved.',
        ['Check or update the graphics driver from the GPU or computer manufacturer.',
         'Check that Minecraft uses a GPU supporting its required OpenGL version.',
         'Review recently changed shaders or graphics mods for this Minecraft version.'],
        'likely'),
    'mixin': (
        'A mod transformation failed',
        'A mixin failed while modifying Minecraft code. An incompatible mod, dependency or conflict may be responsible; the mixin name alone does not prove which mod to remove.',
        ['Check the named mixin or mod and the first Caused by error in the visible logs.',
         'Verify Minecraft, loader and mod versions match; review recently changed mods before making changes.'],
        'likely'),
}


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def analyze(text, context=None):
    """Return JSON-safe guidance, preserving exact bounded lines as evidence.

    Context is a trusted selected-session snapshot: running, exitCode,
    startedAt, logModifiedAt, sourceTruncated and source. Line numbers refer to
    the supplied text, including when this function keeps only its tail.
    """
    context = context if isinstance(context, dict) else {}
    text = text if isinstance(text, str) else ''
    offset = max(0, len(text) - INPUT_LIMIT)
    skipped = text.count('\n', 0, offset)
    tail = text[offset:]
    if offset and offset > 0 and text[offset - 1] != '\n':
        _, separator, tail = tail.partition('\n')
        skipped += bool(separator)
    input_truncated = bool(offset or context.get('sourceTruncated'))
    parts = tail.rsplit('\n', LINE_LIMIT)
    if len(parts) > LINE_LIMIT:
        skipped += parts[0].count('\n') + 1
        tail = '\n'.join(parts[1:])
        input_truncated = True
    records = [(skipped + i + 1, line) for i, line in enumerate(tail.splitlines())]

    # Host logs can contain several sessions. Restrict evidence to the last
    # explicit launch marker rather than making an older failure current.
    start = next((i for i in range(len(records) - 1, -1, -1)
                  if _START.match(records[i][1]) or _FABRIC_START.match(_PREFIX.sub('', records[i][1][:4096], count=1))), None)
    if start is not None:
        records = records[start:]
    running = context.get('running') is True
    exit_code = context.get('exitCode')
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        exit_code = None
    if exit_code is None and not running:
        for _, line in reversed(records):
            match = _EXIT.match(line)
            if match:
                exit_code = int(match.group(1))
                break
    modified, started = _number(context.get('logModifiedAt')), _number(context.get('startedAt'))
    previous = modified is not None and started is not None and modified < started
    scope = 'previous' if previous else ('current' if start is not None or started is not None or running or exit_code is not None else 'unknown')

    findings, seen, evidence_truncated = {}, {}, False
    dependency_until = -1
    for index, (line_number, line) in enumerate(records):
        # Bound regex work as well as evidence. Exceptions have an early marker.
        message = _PREFIX.sub('', line[:4096], count=1).strip()
        lower = message.lower()
        prefix = _PREFIX.match(line[:1024])
        error = bool(prefix and _ERROR.search(prefix.group(0)))
        exception_match = _EXCEPTION.match(message)
        exception = exception_match.group(1) if exception_match else ''
        ids = []
        dependency_header = (error or exception == 'FormattedException') and any(term in lower for term in (
            'incompatible mods found', 'mods are incompatible with the game',
            'missing or unsupported mandatory dependencies', 'missing mandatory dependencies'))
        if dependency_header:
            ids.append('dependency')
            dependency_until = index + 20
        detail = (index <= dependency_until and (
            (lower.startswith('- mod ') and ' requires ' in lower and any(term in lower for term in ('which is missing', 'wrong version', 'only version', 'not installed', 'found version')))
            or (lower.startswith('mod id:') and 'expected range:' in lower and 'actual version:' in lower)))
        direct_requirement = error and ' requires ' in lower and any(term in lower for term in ('which is missing', 'not installed', 'wrong version', 'currently'))
        if detail or direct_requirement:
            ids.append('dependency')
        if (error or exception) and any(term in lower for term in ('duplicate versions for mod id', 'found duplicate mods', 'duplicatemodsfoundexception')):
            ids.append('duplicate-mods')
        if exception == 'UnsupportedClassVersionError':
            ids.append('java-version')
        if exception == 'OutOfMemoryError' or any(lower.startswith(term) for term in (
                '# there is insufficient memory for the java runtime environment to continue',
                'there is insufficient memory for the java runtime environment to continue',
                '# native memory allocation (', 'could not reserve enough space for')):
            ids.append('memory')
        graphics_message = message.split(':', 1)[-1].strip().lower() if exception else lower
        if (error or exception) and (
                re.match(r'glfw error \d+:', graphics_message)
                or any(term in lower for term in ('failed to create window', 'failed to create opengl context',
                                                 'could not create opengl context', 'pixel format not accelerated'))
                or exception == 'LWJGLException' and 'could not create context' in lower):
            ids.append('graphics')
        if (error and ('mixin apply failed' in lower or 'mixin prepare failed' in lower)) or exception in (
                'MixinApplyError', 'MixinTransformerError', 'InvalidMixinException', 'InjectionError', 'InvalidInjectionException'):
            ids.append('mixin')
        for ident in dict.fromkeys(ids):
            if ident not in findings:
                title, explanation, suggestions, confidence = _ADVICE[ident]
                findings[ident] = {'id': ident, 'title': title, 'explanation': explanation,
                                   'suggestions': list(suggestions), 'evidence': [], 'confidence': confidence}
                seen[ident] = set()
            if line in seen[ident] or len(findings[ident]['evidence']) >= EVIDENCE_LIMIT:
                evidence_truncated = True
                continue
            seen[ident].add(line)
            truncated = len(line) > EVIDENCE_TEXT_LIMIT
            findings[ident]['evidence'].append({'line': line_number, 'text': line[:EVIDENCE_TEXT_LIMIT], 'truncated': truncated})
            evidence_truncated |= truncated
            if ident == 'java-version':
                versions = _JAVA.search(message)
                if versions:
                    required, actual = (int(number) - 44 for number in versions.groups())
                    if 5 <= actual < required <= 100:
                        findings[ident]['explanation'] = f'The runtime reports a class requiring Java {required}, but the active runtime supports Java {actual}. Check the named class and the installation’s Java executable.'

    state = 'running' if running else ('successful' if exit_code == 0 else 'failed' if exit_code is not None else 'issues-detected' if findings else 'unknown' if text.strip() else 'no-logs')
    if running:
        summary = 'Minecraft is running. These are logged issues, not a completed crash diagnosis.'
    elif exit_code == 0:
        summary = 'The selected launch exited successfully. Any findings below are logged issues, not a confirmed crash.'
    elif exit_code is not None:
        summary = f'The selected launch exited with code {exit_code}. ' + ('Review the reported evidence and suggestions below.' if findings else 'No common cause was identified. Inspect the first ERROR or Caused by entry and the surrounding visible logs.')
    elif findings:
        summary = 'The selected logs contain recognizable errors. They suggest the issues below; the logs alone do not confirm the cause of a crash.'
    elif text.strip():
        summary = 'No common crash cause was identified. Inspect the first ERROR or Caused by entry and nearby lines; check Minecraft, Java and mod versions.'
    else:
        summary = 'No log entries are available for this installation. Launch Minecraft, then return to Logs to inspect any failure.'
    if previous:
        summary += ' The visible evidence is from a previous session and does not describe the result of the newer launch.'
    if input_truncated:
        summary += ' Only a bounded log tail was analyzed; earlier evidence may be missing.'
    severity = 'info' if previous or exit_code == 0 or not findings and exit_code is None else 'warning' if running else 'error'
    return {'summary': summary, 'severity': severity, 'state': state, 'scope': scope,
            'findings': list(findings.values()), 'truncated': input_truncated or evidence_truncated,
            'inputTruncated': input_truncated, 'evidenceTruncated': evidence_truncated}
