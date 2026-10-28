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
tailscale ip -4                       # say 100.101.102.103
sudo systemctl edit ollama            # then add:
#   [Service]
#   Environment="OLLAMA_HOST=100.101.102.103:11434"
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
curl -s http://100.101.102.103:11434/api/tags      # the model machine answers

adduser --disabled-password kio1 && su - kio1      # not as root
git clone <repository> kio1 && cd kio1
python3 start.py --set-password                    # asks twice; only a hash is kept
```

Tell KIO1 where the models are, in `.env` (which `start.py` makes readable by
its owner only):

```bash
KIO1_PROVIDER_ENDPOINT="http://100.101.102.103:11434"
```

and set `"provider": "ollama"` and a model the machine has in `config.json`.
`KIO1_PROVIDER_ENDPOINT` wins over the file, for the service and for
`start.py`, which checks the model server before starting and, if it does not
answer, says to check Tailscale rather than trying to start Ollama on the
droplet. An endpoint on the public internet over plain HTTP gets a warning.

In DigitalOcean, add a **Cloud Firewall** to the droplet that allows only SSH
(22) inbound — and 80/443 if you choose HTTPS below.

## 3. Reaching the console

**On the tailnet (recommended).** Listen on the droplet's Tailscale address;
only your own devices, signed in to Tailscale, can reach it — phones too:

```bash
python3 start.py --host "$(tailscale ip -4)" --no-browser
# open http://<droplet's Tailscale IP>:8101 from any device on the tailnet
```

**On the internet, with HTTPS.** For people not on your tailnet. KIO1 stays on
`127.0.0.1:8101` and [Caddy](https://caddyserver.com) answers on 443 with a
certificate it obtains itself. Point a domain's A record at the droplet, then:

```bash
sudo apt install caddy
echo 'kio1.example.com {
    reverse_proxy 127.0.0.1:8101
}' | sudo tee /etc/caddy/Caddyfile
sudo systemctl reload caddy
python3 start.py --no-browser
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
ExecStart=/usr/bin/python3 start.py --no-browser --skip-install
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now kio1
journalctl -u kio1 -f
```

For the tailnet option, add `--host <droplet's Tailscale IP>` to `ExecStart`.
Run `python3 start.py` once by hand first, so it creates `.venv` and the
`--skip-install` start has everything it needs.

## Checklist

- [ ] Ollama listens on its Tailscale address, not `0.0.0.0`; the ACL lets only the droplet in.
- [ ] `KIO1_PROVIDER_ENDPOINT` points at the tailnet address; `start.py` sees the model.
- [ ] A console password is set (`start.py --set-password`).
- [ ] The console is on the tailnet, or behind Caddy with HTTPS — not plain HTTP on the internet.
- [ ] The DigitalOcean firewall allows only 22 (and 80/443 for Caddy).
- [ ] KIO1 runs as its own user, not root; `.env` is `600`.
- [ ] Command agents (Pytest, GitHistory) are off unless you need them: they run on the droplet.
