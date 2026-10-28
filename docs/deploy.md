# Running KIO1 on a server

KIO1 on a DigitalOcean droplet (or any Linux server), planning with a model
on another machine — a GPU box everyone on the team shares — reached over
Tailscale. Nothing here needs Docker.

```text
your devices ──(tailnet or HTTPS)──▶ droplet: KIO1 :8101 ──(tailnet)──▶ GPU box: Ollama :11434
```

Two rules hold throughout: **nothing listens on the internet that does not
have to**, and **prompts never cross the internet unencrypted**. Tailscale
gives both for free: traffic between machines on a tailnet is WireGuard, and
an address in `100.64.0.0/10` is unreachable from anywhere else.

## 1. The model machine

Ollama has no password. Let it listen on its Tailscale address only, never
on `0.0.0.0`:

```bash
tailscale ip -4                       # say 100.71.36.18
sudo systemctl edit ollama            # then add:
#   [Service]
#   Environment="OLLAMA_HOST=100.71.36.18:11434"
sudo systemctl restart ollama
ollama pull qwen3.5:4b                # models are pulled here, not from KIO1
```

KIO1 will not download models onto a machine it does not run on — the
console says so — because the disk and the bandwidth are everyone's.

In the Tailscale admin console, restrict who may reach the port. Tag the
droplet `tag:kio1` and the model machine `tag:gpu`, then in the access
controls:

```json
{ "action": "accept", "src": ["tag:kio1"], "dst": ["tag:gpu:11434"] }
```

## 2. The droplet

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --advertise-tags=tag:kio1

adduser --disabled-password kio1 && su - kio1      # not as root
git clone <repository> kio1 && cd kio1
python3 run_with_tailscale.py
```

That is the whole start, every time — no options. `run_with_tailscale.py`:

- finds the droplet's Tailscale address and listens there only, so the
  console is reachable from your devices on the tailnet (phones too) and the
  internet sees no open port — at `http://<machine name>:8101`, the name
  Tailscale gives the droplet (`opeva-cloud-server`, say);
- points the planner at Ollama on the model machine, `100.71.36.18:11434`
  (the address is `MODEL_MACHINE` at the top of the file);
- the first time, asks for the console password and keeps only its hash;
- checks the model machine answers, lists its models, and starts KIO1.

It keeps its choices in `.env` (`KIO1_HOST`, `KIO1_CONSOLE_NAME`, `KIO1_PROVIDER_ENDPOINT`), which
`start.py` makes readable by its owner only — so a plain `python3 start.py`
starts the same way afterwards. To use another model machine, change
`KIO1_PROVIDER_ENDPOINT` there. Pick a model the machine has in the
console's Settings, or in `config.json` (`"provider": "ollama"`).

If the model machine does not answer, KIO1 starts anyway and says to check
Tailscale; it never tries to start Ollama on the droplet. An endpoint on the
public internet over plain HTTP gets a warning.

In DigitalOcean, add a **Cloud Firewall** to the droplet that allows only SSH
(22) inbound — and 80/443 if you choose HTTPS below.

## 3. Reaching the console

**On the tailnet (recommended).** What `run_with_tailscale.py` does: open
`http://opeva-cloud-server:8101` (the droplet's Tailscale name) from any
device signed in to the tailnet. The address KIO1 prints has no token in it:
with a password, signing in is all it takes.

The password is the one typed at the first start; only its hash is kept, so
it cannot be read back. To choose a new one: `python3 start.py --set-password`,
then restart KIO1.

**With a link, for people without Tailscale (Funnel).** Once:

```bash
python3 run_with_tailscale.py --funnel
```

and every later `python3 run_with_tailscale.py` starts the same way (the first
start also asks which of the two you want). KIO1 then listens on 127.0.0.1
only, and [Tailscale Funnel](https://tailscale.com/kb/1223/funnel) publishes it
at `https://opeva-cloud-server.<tailnet>.ts.net` with a certificate Tailscale
manages — send that link. Anyone who has it reaches the sign-in page and
nothing else; the session cookie only travels over HTTPS; and the Funnel is
switched off when KIO1 stops, so the link never points at a stopped server.
`python3 run_with_tailscale.py --tailnet` goes back to the tailnet only.

The first time, Funnel has to be allowed for the tailnet: Tailscale prints a
link to approve it (HTTPS certificates and the `funnel` attribute in the
access controls). If the droplet's user may not change Tailscale's settings,
run `sudo tailscale set --operator=$USER` once.

Everyone shares the one password, sees every conversation, and plans on the
same model machine; per-person accounts are [planned](plans/accounts.md).
Turn the command agents (Pytest, GitHistory) off before sharing: they run on
the droplet.

**On the internet, with your own domain.** For people not on your tailnet. KIO1 stays on
`127.0.0.1:8101` and [Caddy](https://caddyserver.com) answers on 443 with a
certificate it obtains itself. Point a domain's A record at the droplet, then:

```bash
sudo apt install caddy
echo 'kio1.example.com {
    reverse_proxy 127.0.0.1:8101
}' | sudo tee /etc/caddy/Caddyfile
sudo systemctl reload caddy
python3 start.py --host 127.0.0.1 --no-browser
```

Either way the console password is asked for; a stranger reaches nothing but
the sign-in page, a few wrong guesses and that address waits, and the
session cookie is marked `Secure` behind HTTPS. Avoid plain
`--host 0.0.0.0`: the password would cross the internet as typed.

## 4. Keeping it running

Closing the SSH session stops `start.py`. Run it in `tmux`, or as a service:

```ini
# /etc/systemd/system/kio1.service
[Unit]
Description=KIO1
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
User=kio1
WorkingDirectory=/home/kio1/kio1
ExecStart=/usr/bin/python3 run_with_tailscale.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now kio1
journalctl -u kio1 -f
```

Run `python3 run_with_tailscale.py` once by hand first: it creates `.venv`
and asks for the password, which a service cannot do. For the HTTPS option,
use `start.py --host 127.0.0.1 --no-browser` in `ExecStart` instead.

## Checklist

- [ ] Ollama listens on its Tailscale address, not `0.0.0.0`; the ACL lets only the droplet in.
- [ ] `KIO1_PROVIDER_ENDPOINT` points at the tailnet address; `start.py` sees the model.
- [ ] A console password is set (`start.py --set-password`).
- [ ] The console is on the tailnet, or behind Caddy with HTTPS — not plain HTTP on the internet.
- [ ] The DigitalOcean firewall allows only 22 (and 80/443 for Caddy).
- [ ] KIO1 runs as its own user, not root; `.env` is `600`.
- [ ] Command agents (Pytest, GitHistory) are off unless you need them: they run on the droplet.
