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

git clone <repository> kio1 && cd kio1
python3 run_with_tailscale.py
```

That is the whole start, every time — no options, no questions. It prints a
public link, `https://<machine>.<tailnet>.ts.net`; send it, with the console
password, to the people who should use KIO1. They need nothing installed.
What `run_with_tailscale.py` does:

- points the planner at Ollama on the model machine, `100.71.36.18:11434`
  (`MODEL_MACHINE` at the top of the file), unless `.env` names another
  `KIO1_PROVIDER_ENDPOINT`;
- if there is no console password, makes one, prints it, and keeps it in
  `data/console-password`, readable by this user only (`.env` holds only its
  hash; `python3 start.py --set-password` chooses another);
- publishes KIO1 with [Tailscale Funnel](https://tailscale.com/kb/1223/funnel),
  with a certificate Tailscale manages, while KIO1 itself listens on
  127.0.0.1 only. If Tailscale lets only root publish, it makes this user
  Tailscale's operator first — sudo may ask for your password, once;
- starts KIO1, and takes the link down when KIO1 stops.

The very first time on a tailnet, Tailscale may print a link to allow Funnel
(HTTPS certificates and the `funnel` attribute); open it and run the script
again.

Anyone with the link reaches the sign-in page and nothing else; the session
cookie only travels over HTTPS; and wrong guesses are limited. Everyone shares
the one password, sees every conversation, and plans on the same model
machine; per-person accounts are [planned](plans/accounts.md). Turn the
command agents (Pytest, GitHistory) off before sharing: they run on the
droplet.

In DigitalOcean, add a **Cloud Firewall** to the droplet that allows only SSH
(22) inbound: Funnel needs no open port.

## 3. Without Tailscale Funnel

With your own domain instead of a ts.net link: KIO1 stays on
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

The console password is asked for; a stranger reaches nothing but
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

Run `python3 run_with_tailscale.py` once by hand first: it creates `.venv`,
makes the password, and — if sudo asks — makes this user Tailscale's
operator, none of which a service can answer. With Caddy, use
`start.py --host 127.0.0.1 --no-browser` in `ExecStart` instead.

## Checklist

- [ ] Ollama listens on its Tailscale address, not `0.0.0.0`; the ACL lets only the droplet in.
- [ ] `KIO1_PROVIDER_ENDPOINT` points at the tailnet address; `start.py` sees the model.
- [ ] A console password is set (`run_with_tailscale.py` makes one; `start.py --set-password` changes it).
- [ ] The console is published over HTTPS (Funnel, or Caddy) — never plain HTTP on the internet.
- [ ] The DigitalOcean firewall allows only 22 (and 80/443 for Caddy).
- [ ] KIO1 runs as its own user, not root; `.env` is `600`.
- [ ] Command agents (Pytest, GitHistory) are off unless you need them: they run on the droplet.
