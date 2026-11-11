# Uniform DGX Spark platform plugin

This is a native **DeepSeek Harness bundle**. Its root `package.json` declares `dsh.bundle.patch`, so the Harness plugin manager can install it as a managed bundle.

## Install on a colleague's computer

1. Open the DeepSeek Harness Plugins page and choose a local package install.
2. Select `uniform-dgxspark-platform-1.2.3.tgz`.
3. Configure the platform address and API key from a Harness terminal:

```bash
"$DSH_PROFILE_DIR/node_modules/.bin/uniform-dgxspark-configure" \
  --url http://10.21.212.6:18180 \
  --key-file ~/uniform.key
```

Windows PowerShell:

```powershell
& "$env:DSH_PROFILE_DIR\node_modules\.bin\uniform-dgxspark-configure.cmd" `
  --url http://10.21.212.6:18180 `
  --key-file "$HOME\uniform.key"
```

Add `--fallback-url https://uniform.example.com` when the platform is also reachable through a VPN or HTTPS tunnel. The configuration is stored outside the package with mode `0600`:

- Linux / macOS: `~/.config/uniform-dgxspark/platform.env`
- Windows: `%APPDATA%\uniform-dgxspark\platform.env`

Restart or reload the Harness after configuration. Tools appear as `mcp__uniform_dgxspark__*`.

The computer needs Python 3.10 or newer. Set `UNIFORM_PYTHON` to the interpreter path when it is not discoverable automatically.

## Host administrator

Run `bash host/enable-lan-access.sh --show-token` on the DGX Spark to enable authenticated LAN access. Use a VPN or HTTPS tunnel for external access; do not expose plaintext HTTP directly to the Internet.
