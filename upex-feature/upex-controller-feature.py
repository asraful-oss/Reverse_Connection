UPEX_APP_TYPES = {
    'py', 'c', 'cpp', 'cs', 'js', 'ts', 'swift', 'sh', 'bat',
    'exe', 'elf', 'macho', 'bin',
}
UPEX_AUTO_TYPES = {'py', 'cs', 'js', 'sh'}
UPEX_AUTO_APP_TYPES = {'py', 'js', 'cs', 'ts', 'sh'}
UPEX_EXTENSIONS = {
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


def _upex_type_from_name(name):
    return UPEX_EXTENSIONS.get(os.path.splitext(name)[1].lower(), '')


def _upex_os_name(link):
    env = link.env or {}
    return str(env.get('os') or '').strip().lower()


def _upex_detect_type(path, os_name):
    """Detect a type from the extension, then from file magic/shebang."""
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
    if os_name == 'windows':
        return 'unknown'
    return 'bin'


def _upex_auto_path(auto_name):
    """Find auto.<type> next to controller.py (prefer the supplied auto/ dir)."""
    base = os.path.basename(auto_name)
    if base != auto_name or not base.lower().startswith('auto.'):
        return None
    root = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(root, 'auto', base),
                      os.path.join(root, base)):
        if os.path.isfile(candidate):
            return candidate
    return None


def _upex_validate(app_type, os_name, auto_mode):
    if auto_mode:
        if app_type not in UPEX_AUTO_APP_TYPES:
            return ('auto mode accepts app.py, app.js, app.cs, app.ts or '
                    'app.sh')
        return None
    if app_type == 'ts':
        return ('TypeScript needs an auto launcher; use '
                '"upex auto.py app.ts", auto.cs, auto.js or auto.sh')
    if app_type not in UPEX_APP_TYPES:
        return 'unsupported app type'
    if app_type in ('bat', 'exe') and os_name and os_name != 'windows':
        return '.{} files run on Windows only'.format(app_type)
    if app_type == 'elf' and os_name in ('windows', 'macos', 'ios'):
        return '.elf files run on Linux/Android only'
    if app_type == 'macho' and os_name and os_name not in ('macos', 'ios'):
        return '.macho files run on macOS/iOS only'
    if app_type == 'bin' and os_name == 'windows':
        return ('an extensionless executable needs Linux/macOS; use app.exe '
                'on Windows')
    return None


_UPEX_CANCELED = object()


def _upex_upload_one(link, local_path):
    """Upload one file synchronously and return its sanitized remote name.

    Returns _UPEX_CANCELED when the user stopped the upload, and None when the
    upload failed.  The caller must not run the app after a cancel.
    """
    name = os.path.basename(local_path)
    op = Op('upload', next_request_id(), label='upex upload {}'.format(name))
    op.transfer_serial = next(TRANSFER_SERIAL)
    op.transfer_name = name
    op.transfer_kind = 'file'
    op.transfer_status = 'scanning'
    link.add(op)
    print("[+] Uploading {}...".format(name))
    threading.Thread(target=_upload_transfer,
                     args=(link, op, local_path, None),
                     name='upex-upload', daemon=True).start()
    status = wait_foreground(op, cancelable=True, backgroundable=False)
    remote_name = op.transfer_name
    if op.succeeded:
        link.drop(op)
    if status == 'dead' or link.dead:
        return None
    if op.canceled:
        return _UPEX_CANCELED
    if not op.succeeded:
        return None
    return remote_name or name


def _upex_usage():
    print("[!] Usage: upex <app.py|app.c|app.cpp|app.cs|app.js|app.swift|")
    print("               app.sh|app.bat|app.exe|app.elf|app.macho|app>")
    print("    Or:    upex auto.<py|cs|js|sh> <app.py|app.js|app.cs|app.ts|app.sh>")
    print("    Hidden: upex -h auto.<py|cs|js|sh> <app...>")
    print("    The optional auto.<type> launcher is read from the controller's")
    print("    auto/ folder (or the controller folder).")


def _run_upex(link, line):
    """Handle a controller-side upex command. Returns 'done' or 'dead'."""
    parts = split_args(line)
    args = parts[1:]
    hidden = False
    if args and args[0] in ('-h', '--hidden'):
        hidden = True
        args = args[1:]
    if not args or len(args) > 2:
        _upex_usage()
        return 'done'

    auto_arg = None
    if len(args) == 1:
        if hidden:
            _upex_usage()
            return 'done'
        app_arg = args[0]
    else:
        auto_arg, app_arg = args
        if not os.path.basename(auto_arg).lower().startswith('auto.'):
            _upex_usage()
            return 'done'

    app_path = os.path.expanduser(app_arg)
    if not os.path.isabs(app_path):
        app_path = os.path.join(os.getcwd(), app_path)
    app_path = os.path.abspath(app_path)
    if not os.path.isfile(app_path):
        print("[!] Not a local file: {}".format(app_arg))
        return 'done'

    os_name = _upex_os_name(link)
    app_type = _upex_detect_type(app_path, os_name)

    auto_path = None
    auto_type = None
    if auto_arg:
        auto_type = _upex_type_from_name(auto_arg)
        if auto_type not in UPEX_AUTO_TYPES:
            print("[!] Auto mode supports only auto.py, auto.cs, auto.js "
                  "and auto.sh.")
            return 'done'
        auto_path = _upex_auto_path(auto_arg)
        if auto_path is None:
            print("[i] {} was not found in the controller's auto/ folder; "
                  "falling back to normal upex.".format(auto_arg))
            auto_arg = None
            auto_type = None
            if hidden:
                print("[i] Hidden mode needs an auto launcher; running the "
                      "app normally.")
                hidden = False

    error = _upex_validate(app_type, os_name, auto_mode=auto_path is not None)
    if error:
        print("[!] {}".format(error))
        return 'done'

    if auto_path is not None:
        if os.path.basename(app_path) == os.path.basename(auto_path):
            print("[!] The app and auto launcher cannot have the same name.")
            return 'done'

    app_remote = _upex_upload_one(link, app_path)
    if app_remote is _UPEX_CANCELED:
        print("[+] upex canceled.")
        return 'done'
    if app_remote is None:
        if link.dead:
            return 'dead'
        print("[!] Upload failed; nothing was executed.")
        return 'done'

    auto_remote = None
    if auto_path is not None:
        auto_remote = _upex_upload_one(link, auto_path)
        if auto_remote is _UPEX_CANCELED:
            print("[+] upex canceled; the app was not run.")
            return 'done'
        if auto_remote is None:
            if link.dead:
                return 'dead'
            print("[!] Could not upload {}; running the app normally."
                  .format(auto_arg))
            auto_remote = None
            auto_type = None
            hidden = False
            error = _upex_validate(app_type, os_name, auto_mode=False)
            if error:
                print("[!] {}".format(error))
                return 'done'
        elif app_remote == auto_remote:
            print("[!] The uploaded app and auto launcher collided; refusing "
                  "to execute.")
            return 'done'

    label = 'upex {}'.format(app_remote)
    if auto_remote:
        label = 'upex {} {}'.format(auto_remote, app_remote)
    op = Op('upex', next_request_id(), label=label)
    request = {
        'type': 'upex',
        'app': app_remote,
        'app_type': app_type,
        'auto': auto_remote,
        'auto_type': auto_type,
        'hidden': bool(hidden and auto_remote),
    }
    if auto_remote:
        print("[+] Running {} {}{} on the client...".format(
            auto_remote, app_remote, ' (hidden)' if hidden else ''))
    else:
        print("[+] Running {} on the client...".format(app_remote))
    status = ask(link, op, request)
    if status == 'dead':
        return 'dead'
    return 'done'

