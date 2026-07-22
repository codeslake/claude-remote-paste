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
hosts. In a plain remote shell, `Ctrl+V` inserts the image as a file path instead.

Remote requirements: `python3`, `xclip`, `xvfb` (`crimp setup` apt-installs them
when passwordless sudo is available, and tells you what to run otherwise).

## Commands

```
crimp setup [host...]   install crimp + deps + rc wiring on remotes
crimp status            daemon state, hosts, recent log
crimp doctor [host...]  diagnose Mac and remotes
crimp pause | resume    suspend / resume mirroring
crimp push <host>       one-shot manual push (no daemon)
crimp stop              stop the mirror daemon
```

## Config

Environment variables, all optional:

| var | default | |
|---|---|---|
| `CRIMP_HOSTS` | — | ssh destinations to mirror to (space-separated) |
| `CRIMP_KEY` | `^V` | shell-widget key on remotes |
| `CRIMP_DISPLAY` | `:99` | dedicated Xvfb display |
| `CRIMP_USE_HOST_X` | `0` | `1` = reuse an existing X server instead of Xvfb |
| `CRIMP_POLL` | `1` | clipboard poll interval (s) |
| `CRIMP_BACKOFF` | `60` | per-host retry backoff (s) |

## Privacy

Every image you copy is mirrored to all `CRIMP_HOSTS` while the daemon runs
(images only — text never leaves your Mac). The remote clipboard lives in a
dedicated Xvfb locked with xauth, unreadable by other users on shared hosts.
Prefer manual control? Skip the daemon and use `crimp push` / `crimp pause`.

## License

MIT
