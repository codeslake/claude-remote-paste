#!/usr/bin/env python3
"""crimp — Claude Remote IMage Paste.

Copy an image on your Mac, press Ctrl+V in a Claude Code session running over
SSH, and get a real inline [Image #N] attachment. Plain remote shells get a
path-insert widget on the same key.

How: a Mac daemon mirrors clipboard images onto each remote's X clipboard (a
tiny dedicated Xvfb by default, or an existing X server). Claude Code on Linux
reads the clipboard with xclip on Ctrl+V, so paste just works — no key
interception; any terminal, tmux, mosh, or autossh.

stdlib-only single module: the Mac side runs it as an installed uv tool; over
ssh the very same file is shipped to the remote and runs under plain python3.

https://github.com/codeslake/claude-remote-img-paste
"""

import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

__version__ = "0.1.0"

# ---- configuration (all overridable via environment) ------------------------
HOSTS = os.environ.get("CRIMP_HOSTS", "").split()
KEY = os.environ.get("CRIMP_KEY", "^V")            # shell-widget key (zsh caret syntax)
XVFB_DISPLAY = os.environ.get("CRIMP_DISPLAY", ":99")
USE_HOST_X = os.environ.get("CRIMP_USE_HOST_X", "0") == "1"
POLL = float(os.environ.get("CRIMP_POLL", "1"))
BACKOFF = int(os.environ.get("CRIMP_BACKOFF", "60"))
CRIMP_DIR = Path(os.environ.get("CRIMP_DIR", os.path.expanduser("~/.crimp")))

LOG = CRIMP_DIR / "crimp.log"
PIDFILE = CRIMP_DIR / "daemon.pid"
PAUSED = CRIMP_DIR / "paused"
XAUTH = CRIMP_DIR / "xauth"
XVFB_PIDFILE = CRIMP_DIR / "xvfb.pid"

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2"]
IS_MAC = sys.platform == "darwin"


def die(msg, code=1):
    print(f"crimp: {msg}", file=sys.stderr)
    sys.exit(code)


def log(msg):
    CRIMP_DIR.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > 1_048_576:
        LOG.write_text("")
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%m-%d %H:%M:%S')} {msg}\n")


def run(cmd, **kw):
    """subprocess.run with sane defaults; never raises on nonzero exit."""
    kw.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(cmd, **kw)


def ssh(host, argv, stdin_bytes=None, timeout=30):
    """Run a command on host. Returns CompletedProcess; rc 255 on ssh failure."""
    try:
        return subprocess.run(
            ["ssh", *SSH_OPTS, host, *argv],
            input=stdin_bytes,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 255, b"", b"ssh timeout")


def _write_pidfile(pid):
    """Atomic pidfile write (tmp + rename). A plain write_text truncates first,
    and a concurrent ensure reading that empty window sees 'no daemon' and
    spawns a duplicate (observed with 5 concurrent ensures)."""
    tmp = PIDFILE.with_suffix(".tmp")
    tmp.write_text(f"{pid} {module_mtime()}\n")
    os.replace(tmp, PIDFILE)


def module_path():
    return Path(__file__).resolve()


def module_mtime():
    return int(module_path().stat().st_mtime)


# =============================================================================
# Receiver side (Linux): display management
# =============================================================================

def _pid_alive(pidfile, match):
    """pid from pidfile, verified against its command line (pid-recycling guard)."""
    try:
        pid = int(pidfile.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    except OSError:
        p = run(["ps", "-p", str(pid), "-o", "command="], stdout=subprocess.PIPE)
        cmdline = p.stdout.decode()
    return pid if match in cmdline else None


def _xvfb_alive():
    return _pid_alive(XVFB_PIDFILE, "Xvfb") is not None


def _start_xvfb():
    if not shutil.which("Xvfb"):
        return False
    CRIMP_DIR.mkdir(parents=True, exist_ok=True)
    auth_args = []
    if shutil.which("xauth"):
        if not (XAUTH.exists() and XAUTH.stat().st_size > 0):
            cookie = os.urandom(16).hex()
            run(["xauth", "-q", "-f", str(XAUTH), "add", XVFB_DISPLAY, ".", cookie],
                stderr=subprocess.DEVNULL)
        if XAUTH.exists() and XAUTH.stat().st_size > 0:
            auth_args = ["-auth", str(XAUTH)]
    # ponytail: no -auth when xauth is missing — display is then host-open like
    # any default X server; install xauth for a private clipboard.
    proc = subprocess.Popen(
        ["Xvfb", XVFB_DISPLAY, "-screen", "0", "640x480x24", "-nolisten", "tcp", *auth_args],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True)
    XVFB_PIDFILE.write_text(f"{proc.pid}\n")
    sock = Path(f"/tmp/.X11-unix/X{XVFB_DISPLAY.lstrip(':')}")
    for _ in range(20):
        if sock.is_socket():
            return True
        time.sleep(0.2)
    XVFB_PIDFILE.unlink(missing_ok=True)
    return False


def resolve_display():
    """(DISPLAY, XAUTHORITY-or-None) to use; None if no display available.
    Preference: our Xvfb (running, else startable) -> any existing X socket."""
    if not USE_HOST_X and (_xvfb_alive() or _start_xvfb()):
        auth = str(XAUTH) if XAUTH.exists() and XAUTH.stat().st_size > 0 else None
        return XVFB_DISPLAY, auth
    x11 = Path("/tmp/.X11-unix")
    if x11.is_dir():
        for s in sorted(x11.iterdir()):
            if s.is_socket() and s.name.startswith("X"):
                return f":{s.name[1:]}", None
    return None


def setup_display_env():
    disp = resolve_display()
    if not disp:
        return False
    os.environ["DISPLAY"] = disp[0]
    if disp[1]:
        os.environ["XAUTHORITY"] = disp[1]
    return True


def cmd_shellenv(_args):
    """Emit `export ...` lines for eval in a shell rc. No-op on Mac/no display."""
    if IS_MAC:
        return
    disp = resolve_display()
    if not disp:
        return
    print(f"export DISPLAY={disp[0]}")
    if disp[1]:
        print(f"export XAUTHORITY={disp[1]}")


# =============================================================================
# Receiver side (Linux): clipboard verbs
# =============================================================================

def _xclip_targets():
    p = run(["timeout", "3", "xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return p.stdout.decode(errors="replace").split()


def cmd_receive(_args):
    """stdin: PNG bytes -> local X clipboard."""
    if IS_MAC:
        die("receive runs on the Linux receiver")
    if not shutil.which("xclip"):
        die("xclip not installed (apt install xclip)")
    if not setup_display_env():
        die("no display (apt install xvfb, or CRIMP_USE_HOST_X=1)")
    data = sys.stdin.buffer.read()
    if not data:
        die("empty image on stdin")
    f = CRIMP_DIR / "inject.png"
    f.write_bytes(data)
    # xclip -i daemonizes as the selection owner; detach it from this ssh session
    subprocess.Popen(f"xclip -selection clipboard -t image/png -i < '{f}'",
                     shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    time.sleep(0.4)
    if "image/png" not in _xclip_targets():
        die("clipboard verify failed")


def cmd_clear_local(_args):
    """Empty the local X clipboard (stale-image guard)."""
    if IS_MAC:
        die("clear-local runs on the Linux receiver")
    if not shutil.which("xclip") or not setup_display_env():
        return
    subprocess.Popen("xclip -selection clipboard -i < /dev/null",
                     shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, start_new_session=True)


def cmd_paste(_args):
    """X clipboard image -> file, print its path (shell-widget engine)."""
    if IS_MAC:
        die("paste runs on the Linux receiver")
    if not shutil.which("xclip") or not setup_display_env():
        sys.exit(1)
    if "image/png" not in _xclip_targets():
        sys.exit(1)
    d = Path.home() / ".cache/crimp"
    d.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 7 * 86400
    for old in d.glob("paste-*.png"):
        if old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True)
    f = d / f"paste-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.png"
    p = run(["timeout", "3", "xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if p.returncode != 0 or not p.stdout:
        sys.exit(1)
    f.write_bytes(p.stdout)
    print(f, end="")


# =============================================================================
# Sender side (Mac): push / clear / mirror daemon
# =============================================================================

def _remote(host, subcmd, stdin_bytes=None):
    """Run `crimp <subcmd>` on host (uv tool first, ~/.local/bin fallback)."""
    return ssh(host,
               [f'PATH="$HOME/.local/bin:$PATH" crimp {subcmd}'],
               stdin_bytes=stdin_bytes)


def _grab_clipboard():
    """Mac clipboard image as PNG bytes, or None."""
    if not shutil.which("pngpaste"):
        die("pngpaste missing (brew install pngpaste)")
    p = run(["pngpaste", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return p.stdout if p.returncode == 0 and p.stdout else None


def cmd_push(args):
    if not IS_MAC:
        die("push runs on the Mac sender")
    if not args:
        die("usage: crimp push <host>")
    data = _grab_clipboard()
    if not data:
        die("no image in clipboard")
    p = _remote(args[0], "receive", data)
    if p.returncode != 0:
        die(f"push to {args[0]} failed: {p.stderr.decode(errors='replace').strip()}")


def cmd_clear(args):
    if not IS_MAC:
        die("clear runs on the Mac sender")
    if not args:
        die("usage: crimp clear <host>")
    _remote(args[0], "clear-local")


def cmd_daemon_run(_args):
    if not IS_MAC:
        die("the mirror daemon runs on the Mac sender")
    if not HOSTS:
        die("CRIMP_HOSTS is empty")
    if not shutil.which("pngpaste"):
        die("pngpaste missing (brew install pngpaste)")
    CRIMP_DIR.mkdir(parents=True, exist_ok=True)
    _write_pidfile(os.getpid())
    log(f"daemon started (hosts: {' '.join(HOSTS)})")
    down = {h: 0.0 for h in HOSTS}

    def for_hosts(subcmd, data=None):
        for h in HOSTS:
            now = time.time()
            if now < down[h]:
                continue
            p = _remote(h, subcmd, data)
            if p.returncode == 0:
                log(f"{subcmd} -> {h} ok")
            else:
                down[h] = now + BACKOFF
                log(f"{subcmd} -> {h} FAILED (backoff {BACKOFF}s): "
                    f"{p.stderr.decode(errors='replace').strip()[:200]}")

    last_hash, had_img = None, True  # had_img=True: first non-image tick clears remotes
    try:
        while True:
            if PAUSED.exists():
                time.sleep(POLL)
                continue
            data = _grab_clipboard()
            if data:
                h = hashlib.md5(data).hexdigest()
                if h != last_hash:
                    last_hash = h
                    for_hosts("receive", data)
                had_img = True
            elif had_img:
                had_img, last_hash = False, None
                for_hosts("clear-local")
            time.sleep(POLL)
    finally:
        PIDFILE.unlink(missing_ok=True)


def _daemon_pid():
    return _pid_alive(PIDFILE, "crimp")


def _daemon_mtime():
    try:
        return int(PIDFILE.read_text().split()[1])
    except (OSError, ValueError, IndexError):
        return None


def cmd_ensure(_args):
    if not IS_MAC or not HOSTS:
        return
    CRIMP_DIR.mkdir(parents=True, exist_ok=True)
    # atomic mkdir lock: N shells at terminal-session restore would race
    # check-and-spawn into duplicate daemons
    lock = CRIMP_DIR / "ensure.lock"
    try:
        lock.mkdir()
    except FileExistsError:
        if time.time() - lock.stat().st_mtime <= 60:
            return
        try:
            lock.rmdir()
            lock.mkdir()
        except OSError:
            return
    try:
        pid = _daemon_pid()
        if pid is not None:
            if _daemon_mtime() == module_mtime():
                return  # alive & current
            os.kill(pid, 15)  # stale code -> restart
            time.sleep(0.3)
        proc = subprocess.Popen([sys.executable, str(module_path()), "daemon-run"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        # Write the pidfile HERE, not only in daemon-run: python startup takes
        # a few hundred ms, and an ensure arriving in that window would see no
        # live pidfile and spawn a duplicate. daemon-run atomically rewrites
        # the same values on start.
        _write_pidfile(proc.pid)
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass


def cmd_stop(_args):
    pid = _daemon_pid()
    if pid is not None:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    PIDFILE.unlink(missing_ok=True)


def cmd_status(_args):
    pid = _daemon_pid()
    if pid is not None:
        print(f"daemon: running (pid {pid})")
    elif PIDFILE.exists():
        print("daemon: dead (stale pidfile)")
    else:
        print("daemon: not running")
    print(f"mirror: {'PAUSED' if PAUSED.exists() else 'active'}")
    print(f"hosts: {' '.join(HOSTS) or '<unset>'}")
    if LOG.exists():
        print("--- last log lines:")
        print("\n".join(LOG.read_text().splitlines()[-5:]))


# =============================================================================
# Shell integration
# =============================================================================

ZSH_WIDGET = """\
if [[ -o interactive ]]; then
  _crimp_paste_widget() {
    local p
    if p="$(command crimp paste 2>/dev/null)"; then LBUFFER+="$p"
    else zle .quoted-insert; fi
  }
  zle -N _crimp_paste_widget
  bindkey '%KEY%' _crimp_paste_widget
fi
"""

BASH_WIDGET = """\
if [[ $- == *i* ]]; then
  _crimp_paste_insert() {
    local p
    p="$(command crimp paste 2>/dev/null)" || return 0
    READLINE_LINE="${READLINE_LINE:0:READLINE_POINT}$p${READLINE_LINE:READLINE_POINT}"
    (( READLINE_POINT += ${#p} ))
  }
  bind -x '"%KEY%": _crimp_paste_insert'
fi
"""


def cmd_init(args):
    shell = args[0] if args else os.path.basename(os.environ.get("SHELL", "zsh"))
    if IS_MAC:
        # sender: keep the mirror daemon alive from any interactive shell
        print("command -v crimp >/dev/null 2>&1 && crimp ensure 2>/dev/null")
        return
    # receiver: resolve DISPLAY at shell start (claude inherits it), bind widget
    print('command -v crimp >/dev/null 2>&1 && eval "$(crimp shellenv 2>/dev/null)"')
    if shell == "zsh":
        print(ZSH_WIDGET.replace("%KEY%", KEY), end="")
    elif shell == "bash":
        print(BASH_WIDGET.replace("%KEY%", KEY.replace("^", r"\C-")), end="")
    # ponytail: fish/others get no paste widget — claude's own Ctrl+V still
    # works (it needs only DISPLAY, exported above via shellenv).


# =============================================================================
# Setup (Mac -> remote): ship this module, install deps, wire the remote rc
# =============================================================================

# uv bootstrap without touching the remote's shell rc: UV_NO_MODIFY_PATH must
# be on the sh side of the pipe, and the env shims uv drops are removed so no
# orphaned `source ~/.local/bin/env` is left behind. (Pattern credit:
# SAIC-Toronto/skills install.sh.)
REMOTE_ENSURE_UV = r"""
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null 2>&1 && { echo UV_OK; exit 0; }
if command -v curl >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | UV_NO_MODIFY_PATH=1 sh >/dev/null 2>&1
elif command -v wget >/dev/null 2>&1; then
  wget -qO- https://astral.sh/uv/install.sh | UV_NO_MODIFY_PATH=1 sh >/dev/null 2>&1
fi
rm -f "$HOME/.local/bin/env" "$HOME/.local/bin/env.fish" 2>/dev/null
command -v uv >/dev/null 2>&1 && echo UV_INSTALLED || echo UV_FAIL
"""

# stdin: tar of the project source -> uv tool install from it. Same command
# upgrades an existing install (--force).
REMOTE_INSTALL = r"""
export PATH="$HOME/.local/bin:$PATH"
d="$HOME/.local/share/crimp-src"
rm -rf "$d" && mkdir -p "$d" && tar -xf - -C "$d"
uv tool install --force --quiet "$d" 2>&1 | tail -1
command -v crimp >/dev/null 2>&1 && echo CRIMP_OK || echo CRIMP_FAIL
"""

REMOTE_DEPS = r"""
need=""
command -v xclip >/dev/null 2>&1 || need="xclip"
command -v Xvfb  >/dev/null 2>&1 || need="$need xvfb"
[ -z "$need" ] && { echo DEPS_OK; exit 0; }
if sudo -n true 2>/dev/null; then
  sudo apt-get install -y $need >/dev/null 2>&1 && echo DEPS_INSTALLED || echo "DEPS_FAIL:$need"
else echo "DEPS_MANUAL:$need"; fi
"""

REMOTE_RC = r"""
wire() { [ -f "$1" ] && ! grep -qF "crimp init" "$1" \
  && printf '\ncommand -v crimp >/dev/null 2>&1 && eval "$(crimp init %s 2>/dev/null)"\n' "$2" >> "$1"; }
wire ~/.zshrc zsh; wire ~/.bashrc bash; true
"""


def _ok(m): print(f"OK    {m}")
def _warn(m): print(f"WARN  {m}")
def _fail(m): print(f"FAIL  {m}")


def _project_tar():
    """Tar of the project source (pyproject.toml + src/), for remote uv install.
    Works from a dev checkout AND an installed uv tool: uv keeps pyproject next
    to the installed package inside the tool venv? No — so reconstruct minimal
    metadata around this single module when no checkout is found."""
    import io
    import tarfile
    root = module_path().parents[2]  # <root>/src/crimp/__init__.py
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        if (root / "pyproject.toml").is_file():
            tar.add(root / "pyproject.toml", arcname="pyproject.toml")
            tar.add(root / "src" / "crimp", arcname="src/crimp")
        else:
            pyproject = (
                '[project]\nname = "claude-remote-img-paste"\n'
                f'version = "{__version__}"\nrequires-python = ">=3.9"\n'
                '[project.scripts]\ncrimp = "crimp:main"\n'
                '[build-system]\nrequires = ["uv_build>=0.7.19,<1"]\n'
                'build-backend = "uv_build"\n'
                '[tool.uv.build-backend]\nmodule-name = "crimp"\n')
            data = pyproject.encode()
            info = tarfile.TarInfo("pyproject.toml")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            mod = module_path().read_bytes()
            info = tarfile.TarInfo("src/crimp/__init__.py")
            info.size = len(mod)
            tar.addfile(info, io.BytesIO(mod))
    return buf.getvalue()


def cmd_setup(args):
    if not IS_MAC:
        die("setup runs on the Mac sender")
    hosts = args or HOSTS
    if not hosts:
        die("no hosts (crimp setup <host...> or set CRIMP_HOSTS)")
    src = _project_tar()
    for host in hosts:
        print(f"== {host}")
        r = ssh(host, [REMOTE_ENSURE_UV], timeout=120).stdout.decode().strip()
        if r == "UV_OK":
            _ok(f"{host}: uv")
        elif r == "UV_INSTALLED":
            _ok(f"{host}: uv installed")
        else:
            _fail(f"{host}: uv bootstrap failed")
            continue
        p = ssh(host, [REMOTE_INSTALL], stdin_bytes=src, timeout=120)
        if p.returncode != 0 or b"CRIMP_OK" not in p.stdout:
            _fail(f"{host}: install failed: {p.stdout.decode(errors='replace').strip()[:200]}")
            continue
        _ok(f"{host}: crimp installed (uv tool)")
        r = ssh(host, [REMOTE_DEPS]).stdout.decode().strip()
        if r == "DEPS_OK":
            _ok(f"{host}: deps present")
        elif r == "DEPS_INSTALLED":
            _ok(f"{host}: deps installed")
        elif r.startswith("DEPS_MANUAL:"):
            _warn(f"{host}: run 'sudo apt install {r.split(':', 1)[1].strip()}' there")
        else:
            _fail(f"{host}: dep install failed ({r})")
        if ssh(host, [REMOTE_RC]).returncode == 0:
            _ok(f"{host}: rc wired")


# =============================================================================
# Doctor
# =============================================================================

def cmd_doctor(args):
    if IS_MAC:
        _ok("pngpaste") if shutil.which("pngpaste") else _fail("pngpaste missing (brew install pngpaste)")
        _ok(f"CRIMP_HOSTS={' '.join(HOSTS)}") if HOSTS else _warn("CRIMP_HOSTS unset (mirror daemon idle)")
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_status([])
        print("\n".join(f"      {ln}" for ln in buf.getvalue().splitlines()))
        for h in (args or HOSTS):
            if ssh(h, ["true"]).returncode != 0:
                _fail(f"{h}: ssh unreachable")
                continue
            _ok(f"{h}: ssh")
            p = _remote(h, "doctor")
            if p.returncode == 0:
                print("\n".join(f"      [{h}] {ln}" for ln in p.stdout.decode().splitlines()))
            else:
                _fail(f"{h}: crimp not installed on remote (run: crimp setup {h})")
    else:
        _ok("xclip") if shutil.which("xclip") else _fail("xclip missing (apt install xclip)")
        if shutil.which("Xvfb"):
            _ok("Xvfb")
        else:
            _warn("Xvfb missing (apt install xvfb) — will fall back to a host X server")
        _ok("xauth (private display)") if shutil.which("xauth") else _warn("xauth missing — display will be host-open")
        disp = resolve_display()
        if disp:
            _ok(f"display: {disp[0]}{' (xauth-locked)' if disp[1] else ''}")
        else:
            _fail("no display available")


# =============================================================================
# Entry
# =============================================================================

USAGE = f"""\
crimp {__version__} — Claude Remote IMage Paste
https://github.com/codeslake/claude-remote-img-paste

Mac (sender):
  crimp setup [host...]   one-time: install crimp+deps+rc on remotes (default: CRIMP_HOSTS)
  crimp ensure            start the mirror daemon if not running (rc-safe)
  crimp stop|status       stop / inspect the daemon
  crimp pause|resume      suspend / resume mirroring
  crimp push <host>       one-shot: clipboard image -> <host> clipboard
  crimp clear <host>      clear <host>'s clipboard
Linux (receiver, used over ssh / by the shell widget):
  crimp receive           stdin PNG -> X clipboard (starts Xvfb if needed)
  crimp paste             X clipboard image -> file, print path
  crimp shellenv          emit DISPLAY/XAUTHORITY exports for eval
Both:
  crimp init [zsh|bash]   emit shell rc code:  eval "$(crimp init zsh)"
  crimp doctor [host...]  diagnose this machine (and remotes, on Mac)

Config env: CRIMP_HOSTS CRIMP_KEY CRIMP_DISPLAY CRIMP_USE_HOST_X CRIMP_POLL
            CRIMP_BACKOFF CRIMP_DIR
"""

COMMANDS = {
    "setup": cmd_setup, "ensure": cmd_ensure, "stop": cmd_stop,
    "status": cmd_status, "push": cmd_push, "clear": cmd_clear,
    "receive": cmd_receive, "clear-local": cmd_clear_local, "paste": cmd_paste,
    "shellenv": cmd_shellenv, "init": cmd_init, "doctor": cmd_doctor,
    "daemon-run": cmd_daemon_run,
}


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else "help"
    if cmd == "pause":
        CRIMP_DIR.mkdir(parents=True, exist_ok=True)
        PAUSED.touch()
    elif cmd == "resume":
        PAUSED.unlink(missing_ok=True)
    elif cmd == "version":
        print(f"crimp {__version__}")
    elif cmd in ("help", "-h", "--help"):
        print(USAGE, end="")
    elif cmd in COMMANDS:
        COMMANDS[cmd](args[1:])
    else:
        print(USAGE, end="")
        sys.exit(2)


if __name__ == "__main__":
    main()
