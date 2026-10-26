# ---------------------------------------------------------------------------
# upex (upload & execute): the controller uploads the file(s), then sends one
# 'upex' request naming the files already in this directory.  Compilation,
# interpreter selection, fallback order and hidden mode stay on the client so
# every target OS uses its own native commands without shell-quoting guesses
# made on the controller.
# ---------------------------------------------------------------------------

_UPEX_APP_TYPES = {
    'py', 'c', 'cpp', 'cs', 'js', 'ts', 'swift', 'sh', 'bat',
    'exe', 'elf', 'macho', 'bin',
}
_UPEX_AUTO_TYPES = {'py', 'cs', 'js', 'sh'}
_UPEX_EXTENSIONS = {
    '.py': 'py', '.pyw': 'py',
    '.c': 'c',
    '.cc': 'cpp', '.cpp': 'cpp', '.cxx': 'cpp', '.c++': 'cpp',
    '.cs': 'cs',
    '.js': 'js', '.mjs': 'js', '.cjs': 'js',
    '.ts': 'ts',
    '.swift': 'swift',
    '.sh': 'sh', '.bash': 'sh',
    '.bat': 'bat', '.cmd': 'bat',
    '.exe': 'exe',
    '.elf': 'elf',
    '.macho': 'macho',
}


def _upex_simple_name(value):
    """Return a safe single filename for the client's current directory."""
    if not isinstance(value, str):
        return None
    name = value.replace('\\', '/')
    if (not name or name in ('.', '..') or '\x00' in name
            or '/' in name or name.startswith('/')):
        return None
    return name


def _upex_type_from_name(name):
    return _UPEX_EXTENSIONS.get(os.path.splitext(name)[1].lower(), '')


def _upex_detect_type(path, hint=''):
    """Detect an app type, including extensionless ELF/Mach-O/shell files."""
    hint = (hint or '').lower()
    if hint in _UPEX_APP_TYPES:
        return hint
    by_name = _upex_type_from_name(os.path.basename(path))
    if by_name:
        return by_name
    try:
        with open(path, 'rb') as f:
            head = f.read(160)
    except OSError:
        return 'unknown'
    if head.startswith(b'\x7fELF'):
        return 'elf'
    if head[:4] in (b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe',
                    b'\xfe\xed\xfa\xcf', b'\xcf\xfa\xed\xfe',
                    b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'):
        return 'macho'
    if head.startswith(b'MZ'):
        return 'exe'
    if head.startswith(b'#!'):
        first = head.split(b'\n', 1)[0].lower()
        if b'python' in first:
            return 'py'
        if b'node' in first:
            return 'js'
        if b'sh' in first:
            return 'sh'
    # A bare executable is the documented extensionless shell/ELF/Mach-O case
    # on Unix.  Windows needs an explicit .exe (or a file association).
    return 'bin' if os.name != 'nt' else 'unknown'


def _upex_attempt(label, command, run=''):
    """One command to try; run is an optional second command after success."""
    return {'label': label, 'command': command, 'run': run}


def _upex_auto_command(auto, app, auto_type, hidden):
    """Return (attempts, error) for the selected auto launcher.

    New-Feature-Add.txt specifies the exact command names.  Do not probe PATH:
    execute the command and let its own failure drive the documented fallback.
    """
    auto_type = (auto_type or _upex_type_from_name(auto)).lower()
    if auto_type not in _UPEX_AUTO_TYPES:
        return [], 'auto mode supports only py, cs, js and sh'
    flag = ' --hidden' if hidden else ''
    if auto_type == 'py':
        # Use the Windows Python launcher on Windows and the literal python
        # command from the auto.py section elsewhere.  No quotes are added.
        launcher = 'py' if os.name == 'nt' else 'python'
        return [_upex_attempt(
            launcher, '{} {} {} {}'.format(launcher, auto, app, flag))], ''
    if auto_type == 'cs':
        # The '--' belongs to dotnet and is used only for the C# launcher.
        return [_upex_attempt(
            'dotnet', 'dotnet run --file {} -- {} {}'.format(
                auto, app, flag))], ''
    if auto_type == 'js':
        return [_upex_attempt(
            'node', 'node {} {} {}'.format(auto, app, flag))], ''
    # POSIX shell launcher: bash first, then sh, then the executable bit.
    # These are tried by execution failure, not by checking whether they exist.
    attempts = [
        _upex_attempt('bash', 'bash {} {} {}'.format(auto, app, flag)),
        _upex_attempt('sh', 'sh {} {} {}'.format(auto, app, flag)),
    ]
    if os.name != 'nt':
        attempts.append(_upex_attempt(
            'chmod', 'chmod +x {} && ./{} {} {}'.format(
                auto, auto, app, flag)))
    return attempts, ''


def _upex_compiler_candidates(app, app_type):
    """Compile attempts in the exact OS priority order from the spec.

    Availability is never probed.  A failed compiler command is what moves the
    plan to the next documented compiler.
    """
    windows = os.name == 'nt'
    mac = is_macos()
    android = is_android()
    ios = is_ios()
    base = os.path.splitext(os.path.basename(app))[0]
    output = base + ('.exe' if windows else '')
    run = '.\\' + output if windows else './' + output
    if app_type == 'c':
        if windows:
            return [
		_upex_attempt('cl', 'cl /Fe:{} {}'.format(output, app), run),
		_upex_attempt('gcc', 'gcc {} -o {}'.format(app, output), run)
            ]
        if android:
            # Termux normally provides clang, but keep the existing gcc-first
            # order and fall back only after a real failure.
            return [
                _upex_attempt('gcc', 'gcc {} -o {}'.format(app, output), run),
		_upex_attempt('clang', 'clang {} -o {}'.format(app, output), run)
            ]
        if mac or ios:
            return [
                _upex_attempt('clang', 'clang {} -o {}'.format(app, output), run),
                _upex_attempt('gcc', 'gcc {} -o {}'.format(app, output), run)
            ]
        return [
            _upex_attempt('gcc', 'gcc {} -o {}'.format(app, output), run)
        ]

    if app_type == 'cpp':
        if windows:
            return [
                _upex_attempt('cl', 'cl /EHsc /Fe:{} {}'.format(output, app), run),
                _upex_attempt('g++', 'g++ {} -o {}'.format(app, output), run)
            ]
        if android:
            return [
                _upex_attempt('g++', 'g++ {} -o {}'.format(app, output), run),
                _upex_attempt('clang++', 'clang++ {} -o {}'.format(app, output), run)
            ]
        if mac or ios:
            return [
                _upex_attempt('clang++', 'clang++ {} -o {}'.format(app, output), run)
            ]
        return [
            _upex_attempt('g++', 'g++ {} -o {}'.format(app, output), run)
        ]
    return []


def _upex_app_plan(app, app_type, hidden):
    """Build the exact OS command attempts for one app.

    No interpreter or compiler is looked up in PATH.  Commands are constructed
    from New-Feature-Add.txt and executed as written; only the documented
    alternatives are attempted after a real command failure.
    """
    app_type = (app_type or _upex_type_from_name(app)).lower()
    windows = os.name == 'nt'
    mac = is_macos()
    ios = is_ios()

    if hidden:
        return None, 'hidden mode is only available with an auto launcher'

    if app_type == 'py':
        launcher = 'py' if windows else 'python3'
        return {'attempts': [
            _upex_attempt(launcher, '{} {}'.format(launcher, app))]}, ''
    if app_type == 'js':
        return {'attempts': [
            _upex_attempt('node', 'node {}'.format(app))]}, ''
    if app_type == 'cs':
        # dotnet first; csc is only reached if dotnet actually fails.
        output = os.path.splitext(os.path.basename(app))[0] + '.exe'
        run = '.\\' + output if windows else 'mono ' + output
        return {'attempts': [
            _upex_attempt('dotnet', 'dotnet run --file {}'.format(app)),
            _upex_attempt('csc', 'csc /out:{} {}'.format(output, app), run),
        ]}, ''
        
    if app_type in ('c', 'cpp'):
        attempts = _upex_compiler_candidates(app, app_type)
        if not attempts:
            return None, 'unsupported {} compiler'.format(app_type.upper())
        return {'attempts': attempts}, ''
    if app_type == 'swift':
        base = os.path.splitext(os.path.basename(app))[0]
        output = base + ('.exe' if windows else '')
        run = '.\\' + output if windows else './' + output
        return {'attempts': [
            _upex_attempt('swift', 'swift {}'.format(app)),
            _upex_attempt('swiftc', 'swiftc {} -o {}'.format(app, output), run),
        ]}, ''
    if app_type == 'sh':
        if windows:
            # Not part of the Windows matrix, but keep the existing support
            # without probing: bash, then sh, both executed literally.
            return {'attempts': [
                _upex_attempt('bash', 'bash {}'.format(app)),
                _upex_attempt('sh', 'sh {}'.format(app)),
            ]}, ''
        return {'attempts': [
            _upex_attempt('chmod', 'chmod +x {} && ./{}'.format(
                app, app))]}, ''
    if app_type == 'bat':
        if not windows:
            return None, '.bat files run on Windows only'
        return {'attempts': [
            _upex_attempt('cmd', '.\\' + app)]}, ''
    if app_type == 'exe':
        if not windows:
            return None, '.exe files run on Windows only'
        return {'attempts': [
            _upex_attempt('cmd', '.\\' + app)]}, ''
    if app_type == 'elf':
        if windows or mac or ios:
            return None, '.elf files run on Linux/Android only'
        return {'attempts': [
            _upex_attempt('chmod', 'chmod +x {} && ./{}'.format(
                app, app))]}, ''
    if app_type == 'macho':
        if not (mac or ios):
            return None, '.macho files run on macOS/iOS only'
        return {'attempts': [
            _upex_attempt('chmod', 'chmod +x {} && ./{}'.format(
                app, app))]}, ''
    if app_type == 'bin':
        if windows:
            return None, ('an extensionless executable needs Linux/macOS; '
                          'use app.exe on Windows')
        return {'attempts': [
            _upex_attempt('chmod', 'chmod +x {} && ./{}'.format(
                app, app))]}, ''
    if app_type == 'ts':
        return None, ('TypeScript needs an auto launcher; use '
                      '"upex auto.py app.ts", auto.cs, auto.js or auto.sh')
    return None, 'unsupported app type'


def _upex_append(parts, text):
    text = (text or '').strip()
    if text:
        parts.append(text)


def _upex_execute_attempts(conn, req_id, current_dir, attempts):
    """Try commands in order; stop at the first success.

    A successful compile attempt with a ``run`` command immediately runs that
    binary and returns its status.  A failed command advances to the next
    documented alternative.  No PATH check happens before any command.
    """
    parts = []
    last_code = None
    for index, attempt in enumerate(attempts):
        text, stop, code = run_command(
            conn, attempt['command'], current_dir, req_id)
        _upex_append(parts, text)
        if stop or code is None:
            return '\n'.join(parts), stop, code
        last_code = code
        if code == 0:
            run = attempt.get('run')
            if not run:
                return '\n'.join(parts), False, 0
            text, stop, code = run_command(conn, run, current_dir, req_id)
            _upex_append(parts, text)
            return '\n'.join(parts), stop, code
        if index + 1 < len(attempts):
            parts.append('[upex] {} failed (exit {}); trying the next option'
                         .format(attempt['label'], code))
    if not parts:
        parts.append('[upex] no command could run the app')
    return '\n'.join(parts), False, last_code


def run_upex(conn, msg, current_dir, req_id=None):
    """Handle one 'upex' request after the controller uploaded the file(s)."""
    app = _upex_simple_name(msg.get('app'))
    if not app:
        return 'upex: unsafe or missing app filename', False, 1
    app_path = os.path.join(current_dir, app)
    if not os.path.isfile(app_path):
        return 'upex: file not found: {}'.format(app), False, 1
    app_type = _upex_detect_type(app_path, msg.get('app_type'))
    if app_type not in _UPEX_APP_TYPES:
        return 'upex: unsupported app type for {}'.format(app), False, 1

    auto = msg.get('auto')
    hidden = bool(msg.get('hidden'))
    if auto:
        auto = _upex_simple_name(auto)
        if not auto:
            return 'upex: unsafe or missing auto filename', False, 1
        auto_path = os.path.join(current_dir, auto)
        if not os.path.isfile(auto_path):
            return 'upex: auto file not found: {}'.format(auto), False, 1
        auto_type = (msg.get('auto_type') or
                     _upex_type_from_name(auto)).lower()
        attempts, error = _upex_auto_command(auto, app, auto_type, hidden)
        if error:
            return 'upex: {}'.format(error), False, 1
        text, stop, code = _upex_execute_attempts(
            conn, req_id, current_dir, attempts)
        if code not in (None, 0):
            note = '[upex] auto launcher exited with status {}'.format(code)
            text = '{}\n{}'.format(text, note) if text.strip() else note
        return text, stop, code

    if hidden:
        return 'upex: --hidden is only available with an auto launcher', False, 1
    plan, error = _upex_app_plan(app, app_type, hidden)
    if error:
        return 'upex: {}'.format(error), False, 1
    return _upex_execute_attempts(
        conn, req_id, current_dir, plan['attempts'])
