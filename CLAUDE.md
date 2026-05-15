# CLAUDE.md — parts-bot

> Repo-specific context for Claude Code sessions. Cross-project info lives in [molina-vault](https://github.com/donnamolina/molina-vault).

## What this is
WhatsApp bot for insurance claim parts lookups (Pieza Finder). Translates Dominican Spanish part names to OEM English, looks up part numbers via 7zap relay (residential IP required), prices via eBay Browse API, computes landed cost.

## Stack
- Node.js + Baileys + Claude
- eBay Browse API
- 7zap relay on Mac mini (port 8765, residential IP required)

## Conventions
- Architecture: `agentLoop()` with three tools — `log_correction`, `search_parts`, `close_session`
- Landed cost: `parts_cost_usd × 63 + weight_lbs × 246` (RD$63/USD, RD$246/lb shipping)
- 7zap relay URL: via Cloudflare Tunnel to Mac mini port 8765 — `cf_clearance` cookie is IP-locked
- Related parked relay: `pieza-relay-partsouq` (PartSouq, port 8766) — not deployed

## Deployment
Droplet B, PM2 id 0, `/opt/parts-bot/server.js`. SSH: `ssh droplet-b`.

## Don't
- Don't call 7zap directly from the bot — always go through the Mac mini relay (cookie is IP-locked to residential IP)
- Don't hardcode FX rate (RD$63) or shipping rate (RD$246/lb) — they're config values
- Don't bypass `log_correction` — the DR Spanish parts dictionary improves only through that tool
- Don't introduce a second parts-name dictionary — one source of truth

## Open TODOs
- [ ] Confirm whether parts-bot uses Supabase and which project ref
- [ ] Consider restricting relay to known IPs

## More context
- Vault: `projects/parts-bot.md` in `donnamolina/molina-vault`
- Infrastructure: `infra/droplet-b.md`, `infra/mac-mini-donna.md`

## Auto-log substantive work to the vault

When you complete any substantive work in this repo — a deploy, a bug fix, a meaningful refactor, a schema change, a new feature shipped — append a one-line entry to the corresponding vault page's History section and commit the vault.

**Trigger:** substantive = anything you'd mention to a teammate at standup. Not: typo fixes, comment edits, dependency bumps, formatting-only changes.

**Where:** `~/molina-vault/projects/parts-bot.md` — find the `## History` section and add a line at the top of its list.

**Format:** `- YYYY-MM-DD: <one-line summary of what changed and why it matters>`

**Commit:** `cd ~/molina-vault && git add projects/parts-bot.md && git commit -m "auto: parts-bot — <summary>"`

**Don't push the vault** unless I explicitly ask. Local commits are fine; pushing is my call.

**If the vault page doesn't exist** (new project, etc.), stop and ask me before creating one — vault structure is intentional.

**If you're unsure whether something counts as substantive**, ask. Better to ask once than spam the vault with noise or skip something that mattered.
