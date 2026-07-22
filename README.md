# claude-remote-img-paste (crimp)

Paste images from your Mac clipboard into a Claude Code session running over SSH.
Copy an image, press `Ctrl+V` in the remote session, get a real inline `[Image #N]`
attachment — exactly like a local paste.

## Why

Claude Code on Linux reads the clipboard with `xclip` on `Ctrl+V`, which needs an
X display — a headless SSH box has neither, so image paste silently fails
([#42712](https://github.com/anthropics/claude-code/issues/42712)).

`crimp` fixes this from the Mac side: a small daemon mirrors your clipboard images
onto each remote's X clipboard (a tiny dedicated Xvfb, xauth-locked). By the time
you press `Ctrl+V`, the image is already there — no key interception, works from
any terminal, tmux, mosh, or autossh.

```
Mac clipboard ──(mirror daemon, ~2s)──▶ remote Xvfb clipboard ──(Ctrl+V)──▶ [Image #1]
```

## Install

```sh
uv tool install git+https://github.com/codeslake/claude-remote-img-paste
```

One-time setup (installs crimp + deps on every remote over ssh, wires shell rc):

```sh
crimp setup <host> [host...]
```

Add to your Mac `~/.zshrc` (or `~/.bashrc`):

```sh
export CRIMP_HOSTS="<host> [host...]"
eval "$(crimp init)"
```

That's it. Copy an image, press `Ctrl+V` in a Claude Code session on any of those
hosts.

crimp never intercepts keys inside Claude Code — it only keeps the remote
clipboard in sync, and Claude's own paste handler reads it. If you've rebound
Claude's paste key, crimp works with whatever key that is, no configuration.

Remote requirements: `python3`, `xclip`, `xvfb` (`crimp setup` apt-installs them
when passwordless sudo is available, and tells you what to run otherwise).

## Bonus: paste in a plain shell (no Claude)

A shell has no notion of pasting an image, so crimp also ships a small zsh/bash
widget on the remote: at a plain prompt, `Ctrl+V` saves the clipboard image to a
file and inserts its path. This is the only key crimp owns anywhere — it lives
in the *shell*, not in Claude Code. Since `Ctrl+V` normally means quoted-insert
in a shell (the widget falls through to it when there's no image), you can move
the widget to another key with `CRIMP_KEY` (zsh caret syntax, e.g. `^G`), or
ignore this feature entirely — Claude paste is unaffected either way.

## Commands

```
crimp setup [host...]   install crimp + deps + rc wiring on remotes
crimp ensure            start the mirror daemon if not running (rc-safe)
crimp status            daemon state, hosts, recent log
crimp doctor [host...]  diagnose Mac and remotes
crimp pause | resume    suspend / resume mirroring
crimp push <host>       one-shot manual push (no daemon)
crimp clear <host>      clear a remote clipboard
crimp stop              stop the mirror daemon
crimp init [zsh|bash]   emit the shell rc code (used by the rc line)
crimp version           print version
```

## Config

Environment variables, all optional:

| var | default | |
|---|---|---|
| `CRIMP_HOSTS` | — | ssh destinations to mirror to (space-separated) |
| `CRIMP_DISPLAY` | `:99` | dedicated Xvfb display |
| `CRIMP_USE_HOST_X` | `0` | `1` = reuse an existing X server instead of Xvfb |
| `CRIMP_POLL` | `1` | clipboard poll interval (s) |
| `CRIMP_BACKOFF` | `60` | per-host retry backoff (s) |
| `CRIMP_DIR` | `~/.crimp` | state directory (log, pidfiles, xauth) |
| `CRIMP_KEY` | `^V` | plain-shell widget key only (see Bonus above) — not related to Claude Code's paste key |

## Privacy

Every image you copy is mirrored to all `CRIMP_HOSTS` while the daemon runs
(images only — text never leaves your Mac). The remote clipboard lives in a
dedicated Xvfb locked with xauth, unreadable by other users on shared hosts;
crimp's state files are 0600 in a 0700 directory, and the transferred image
is deleted from disk once the clipboard owns it. If `xauth` is missing on a
host, the Xvfb display is as open as any default X server — `crimp doctor`
warns about this. Prefer manual control? Skip the daemon and use
`crimp push` / `crimp pause`.

Note: `crimp setup` appends one line to the remote `~/.zshrc` / `~/.bashrc`
(idempotent — it skips files that already have it). If your rc files are
symlinks into a dotfiles repo, that append lands in the repo; wire the line
through your dotfiles instead and setup will leave it alone.

## License

MIT
