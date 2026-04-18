# Nexus Agent — Launch Day Playbook (internal)

> **What this is.** A runbook for launch day. Treat the launch as a
> 48-hour incident-response exercise, not a marketing event. The goal
> is not "go viral"; the goal is *zero operator failures while under
> public attention*. Virality is a second-order effect of the repo
> holding up under load and the claims holding up under scrutiny.
>
> **What this is NOT.** The HN / Reddit / X post copy itself — that's
> a separate deliverable, last item in
> [MEMORY_FLAGSHIP_PLAN.md](../MEMORY_FLAGSHIP_PLAN.md) Week 4.
> Success-metric discussion — see
> [MEMORY_FLAGSHIP_PLAN.md §2.5](../MEMORY_FLAGSHIP_PLAN.md) and the
> Phase 13 gate (500+ stars, HN front page, three trusted reviewers).
> Marketing strategy — if you're thinking about ad spend or influencer
> outreach, you are working on the wrong Nexus.

This doc is internal-only. Do not link it from the README.

---

## Contents

1. [Philosophy — receipts over hype](#philosophy--receipts-over-hype)
2. [T-minus calendar](#t-minus-calendar)
3. [Launch sequencing — the posting order](#launch-sequencing--the-posting-order)
4. [What to monitor in the first 2 hours](#what-to-monitor-in-the-first-2-hours)
5. [Pre-drafted replies to the four most likely objections](#pre-drafted-replies-to-the-four-most-likely-objections)
6. [Fallbacks — what to do when something goes wrong](#fallbacks)
7. [Exit criteria — when is the launch "over"](#exit-criteria)
8. [Post-launch: the 7-day operator checklist](#post-launch-the-7-day-operator-checklist)
9. [Anti-patterns — what not to do](#anti-patterns)

---

## Philosophy — receipts over hype

Three rules that govern every decision in this playbook:

1. **Every claim is one `curl` / `pytest` / `make` away from being
   falsifiable.** That's why the README, the benchmark doc, the
   integration docs, and the screencast all end with single-command
   reproductions. Before launching, re-verify that a fresh clone can
   do every command in the README in under five minutes. If anything
   in the README has bitrotted, fix it. This is more important than
   any launch copy.
2. **The launch is about the software being good under scrutiny, not
   the post being witty.** A mediocre post on a great repo outperforms
   a great post on a mediocre repo every time. If you catch yourself
   optimising the HN title instead of fixing an open issue, pivot
   back.
3. **Plan for the thing you can't plan for.** The single most likely
   launch-day failure is an operator email you didn't anticipate
   ("your Docker image is broken on arm64", "your Postgres migration
   drops a column", "your rate limiter is advertised as Redis but
   falls back silently"). The fallbacks table below encodes the
   playbook for that. Rehearse it.

---

## T-minus calendar

All times in operator local TZ. Launch day is called "L". "L-7d"
means "seven days before launch". Everything marked **mandatory**
blocks the launch if it fails. Everything marked *nice-to-have* can
slip.

### L-7d — one week out

**Mandatory:**

- [ ] **Phase 12B exit gate is green.** Every checkbox in
  [MEMORY_FLAGSHIP_PLAN.md §"Phase 12B Exit Gate"](../MEMORY_FLAGSHIP_PLAN.md)
  passes: agent-with-memory uplift ≥ 10%, skill composition ≥ 85%,
  tool-injection red-team 100% block, causal QA 100%, temporal QA
  100%, contradiction QA 100%. If any is red, the launch slips a
  week — *an underpowered launch is worse than a delayed one*.
- [ ] **Nightly benchmark workflow has run green 7 days in a row.**
  Check [`.github/workflows/nightly_benchmark.yml`](../.github/workflows/nightly_benchmark.yml)
  runs in GitHub Actions. If any run is red, stop and fix.
- [ ] **`make dev` on a fresh Ubuntu 22.04 + Python 3.13 VM** takes
  under five minutes from `git clone` to a responding `/health`.
  Time it with a stopwatch; record the number in this doc.
  Current time: **~4m 30s** (update this value before launch).
- [ ] **Three trusted reviewers** have read the draft Show HN post,
  the README hero, and clicked through the screencast. Their
  feedback has been merged. Not a forced three-tier sign-off; they
  just need to say "yes, this is honest, yes, it stands up."

*Nice-to-have:*

- [ ] Dogfooding — you've been running Nexus in anger for ≥30 days
  for your own daily agent needs (OpenClaw skills, Hermes routing,
  memory queries). If this isn't true, you don't have the authority
  to claim the thing works; fix that first.

### L-2d — two days out

**Mandatory:**

- [ ] Verify the ten items in `make dev` → `pytest` → every
  single-command reproduction in `README.md` → every benchmark in
  `docs/benchmarks.md`. **Twenty minutes; physical stopwatch.** Any
  failure = launch slips.
- [ ] The live `nexus.denialweb.com` (or wherever the public
  landing lives) is serving `/health` with 200 and the dashboard is
  reachable. If there's no public demo: skip. If there is: *a 502
  at launch is the worst thing that can happen*.
- [ ] DNS TTL for any public domain is ≤ 300s **already** (change it
  now if it isn't — propagation takes 24h).
- [ ] The GitHub repo description + topics are set.
  `github.com/denial-web/nexus-agent` shows "The governed,
  self-improving agent runtime…" as its one-liner. Topics include:
  `ai-agent`, `llm`, `prompt-injection`, `governance`,
  `openclaw`, `hermes`.
- [ ] README passes the *5-second test* on someone who has never
  heard of Nexus. Show them the top 800px; if they can't tell you
  what the project does in their own words, the hero is still broken.
- [ ] Docker image is built, tagged, and pushed to both Docker Hub
  and `ghcr.io`. `docker run ...` on a fresh VM works. Write down
  the command.
- [ ] On-call phone / laptop charged, VPN configured, GitHub + HN
  + Twitter credentials verified (not just cached — *verified*).

*Nice-to-have:*

- [ ] A screencast upload exists on YouTube (unlisted) with a direct
  share URL. YouTube thumbnail is set. Captions auto-generated and
  corrected.

### L-1d — one day out

**Mandatory:**

- [ ] **Feature freeze.** No merges to `master` for the next 72
  hours unless they fix a launch-blocking bug. A shiny new commit
  at L+2h is a foot-gun.
- [ ] Run `git log --oneline origin/master..HEAD` — should be empty.
  If not, decide now: do you push or hold?
- [ ] Draft all five social posts (HN, Reddit r/MachineLearning,
  Reddit r/LocalLLaMA, X/Twitter thread, LinkedIn) and put them in
  a single `launch-drafts.md` gist in a PRIVATE gist. Do NOT post
  them yet.
- [ ] Pre-schedule NOTHING. Every post goes up manually on launch
  day so you can adapt copy to what's already live.
- [ ] Sleep. You cannot operator-on-call tired.

### L-day — launch day

- **T-90min:** Wake up, coffee, re-read this doc and the three top
  objections below. Open the dashboards in separate tabs (see
  [What to monitor](#what-to-monitor-in-the-first-2-hours)).
- **T-60min:** One final smoke test: `git clone` into `/tmp`,
  `make dev`, hit `/health`. If green, proceed. If red, **abort and
  slip 24 hours** — DO NOT launch with a broken quickstart. The
  cost of a delay is invisible; the cost of a broken launch is
  permanent.
- **T-10min:** Clear your calendar, put your phone on DND except
  for the on-call pager, close Slack DMs, open a scratch pad.
- **T-0:** Post to HN. See [Launch sequencing](#launch-sequencing--the-posting-order).

### L+2h, L+24h, L+7d

Covered in [What to monitor](#what-to-monitor-in-the-first-2-hours)
and [Post-launch](#post-launch-the-7-day-operator-checklist).

---

## Launch sequencing — the posting order

Do these in order. Do not parallelize. Each step gives the next
step context the audience will pick up on ("I saw it on HN, the
author is also here on X"). Posting everywhere simultaneously looks
like astroturfing and breaks the momentum arc.

### T-0 — Hacker News, Show HN

**URL:** https://news.ycombinator.com/submit

**Why first:** HN is the audience most likely to actually read the
repo, run the benchmarks, and find bugs. If the repo can't survive
HN scrutiny, you need to know before posting anywhere else. HN is
also the single highest-signal channel for reaching the target
audience (AI infra engineers, governance engineers, security-aware
ML folks).

**Title format:** `Show HN: Nexus Agent – <8-word-differentiator>`

Load-bearing word: "governed" or "runtime" (from the positioning
headline in [MEMORY_FLAGSHIP_PLAN §2.5](../MEMORY_FLAGSHIP_PLAN.md)).
Do not editorialize ("blazing-fast", "beautiful", "revolutionary"
— all tank HN posts). Do not tease ("you won't believe").

**Body:** The HN post draft lives in `launch-drafts.md` (see
L-1d checklist). It must end with the four killer-demo links, the
benchmark doc link, and an invitation to break it ("I'll be here
all day to answer questions and fix bugs").

**Immediate after-action (T+0 to T+15min):**

- Do NOT upvote your own post. It's detectable and gets you flagged.
- Do NOT message friends asking for upvotes. Same reason.
- Open the HN thread in one tab; leave it. Do not refresh obsessively.
- Check `/health` on the demo host. If 200, proceed.

### T+30min — X / Twitter thread

**Why now:** By T+30min, the HN post has either caught traction
(top 30 on `/new`) or hasn't. Either way, the X amplification now
points at a LIVE HN thread people can engage with, which is better
than posting in a vacuum.

**Thread structure (5-7 posts, not more):**

1. The positioning headline, one sentence, one line.
2. The four killer demos — one per post, with the single-command
   repro verbatim. Use real screenshots or terminal captures, not
   stylized mockups.
3. The Show HN link ("Full discussion on HN: …").
4. The repo link.

**Do NOT:**

- Use >3 hashtags in the whole thread. One is fine.
- Tag influencers who didn't ask to be tagged.
- Pin the thread yet (pin later, after you know which performs).

### T+60min — Reddit r/LocalLLaMA

**Why third:** r/LocalLLaMA is a narrower audience than HN, highly
skeptical, and rewards technical depth. Post once HN is warm so
anyone who checks finds discussion already in progress.

**Title:** `[Project] Nexus Agent: zero-trust runtime for
OpenClaw/Hermes — governance, memory, hash-chained audit`.

**Body:** Shorter than HN (300 words max). Focus on the Hermes
integration angle because that's r/LocalLLaMA's home ground. Link
to `docs/hermes_integration.md` directly.

**Expected questions to pre-answer in the post:**
- "Why not use OpenAI's native tool-calling?" → link to
  [docs/hermes_integration.md §"Why plain JSON, not OpenAI function-calling"](hermes_integration.md#the-tool-call-protocol--plain-json-not-openai-tools-api).
- "Can I run this fully local?" → yes, `LOCAL_ONLY=true`; link to
  the env section of the README.

### T+90min — Reddit r/MachineLearning

**Why fourth and only if HN is tracking well:** r/ML has strict
"is this actually research?" norms. If HN has already generated a
thread with substantive technical discussion, r/ML is receptive;
if HN is cold, r/ML will drag the post.

**Title:** `[P] Nexus Agent: governed runtime for agentic LLMs —
benchmark receipts inside`.

**Body:** Lead with the six benchmark exit gates from
[docs/benchmarks.md](benchmarks.md). Tables, not prose. Link the
Show HN discussion.

**Abort condition:** If HN post is below 30 karma at T+90min, SKIP
r/ML. Post it 24 hours later once the HN thread has a final score.

### T+2h — LinkedIn (optional)

**Only if:** you have a professional LinkedIn presence that maps to
the target audience (CISO / platform eng / AI governance).
Otherwise skip — LinkedIn is low-signal for OSS infrastructure.

### T+24h — Show HN repost / follow-up

**Only if** the original Show HN did NOT reach the front page. HN
rules allow a single re-submission if the original was posted at a
bad time; don't abuse this. Post the repost at a different TZ peak
(if launch was US-morning, repost UK-morning).

---

## What to monitor in the first 2 hours

Open these in a dedicated browser window, side-by-side. Do NOT
multi-task on Slack / email during this window.

| Panel | URL | Watch for |
|---|---|---|
| HN Show HN | https://news.ycombinator.com/shownew | Your post climbing or dropping from `/shownew` to `/front` |
| HN post thread | (your submission URL) | New comments — respond to every substantive one within 10min |
| GitHub repo traffic | https://github.com/denial-web/nexus-agent/pulse | Unique clones / visitors spike matches HN traffic |
| GitHub Stars | https://github.com/denial-web/nexus-agent/stargazers | Rate-of-change, not absolute — a star spike at T+10min means HN is working |
| GitHub Issues | https://github.com/denial-web/nexus-agent/issues | Every new issue opened in first 48h = P0 triage |
| Demo `/health` | https://nexus.denialweb.com/health (or wherever) | 200 green; if 503, see [Fallbacks](#fallbacks) |
| Demo `/health/ready?deep=true` | same | All provider probes green; CB status `closed` |
| Prometheus `/metrics` | authed | `nexus_http_request_duration_seconds` p95 < 2s |
| X/Twitter mentions | search for `github.com/denial-web/nexus-agent` | Retweets, quote-tweets, tagged mentions |
| Reddit thread | (once posted) | Same as HN — substantive comment ⇒ 10min response |

**Green-state definition.** All of the following are true:

- HN post is climbing on `/shownew`.
- `/health` is 200.
- `/metrics` p95 latency is under 2s.
- No issue opened in the last 15min tagged `bug`.

**Yellow-state definition** (two of):

- HN post static for 20min at a fixed rank (could go either way).
- A single opened issue claiming a reproducible bug.
- Demo host CPU > 80% sustained.

**Red-state definition** (any of):

- HN post flagged or [dead] — see [Fallbacks](#fallbacks).
- `/health` 5xx for > 60s.
- A security vulnerability disclosed in an issue or a DM.

---

## Pre-drafted replies to the four most likely objections

These are the objections that will appear top-of-thread within the
first hour, based on the positioning doc in
[MEMORY_FLAGSHIP_PLAN.md §2.5](../MEMORY_FLAGSHIP_PLAN.md). Responses
are deliberately short, receipt-bearing, non-defensive.

### Objection 1: "This is just LangChain / CrewAI / AutoGen."

**Canned response:**

> It isn't. LangChain/CrewAI/AutoGen are frameworks you write your
> agent *in*. Nexus is a runtime you run *any* agent *inside* —
> including OpenClaw skills and any Hermes-class tool-calling model
> — with prompt-injection scanning in 11 languages, default-deny
> governance with K-of-N approval, a critic tree, and hash-chained
> audit on every step. The distinction is framework vs runtime, not
> framework-vs-framework. `README.md` has a 4-row positioning table
> if you want the explicit comparison — `docs/openclaw_integration.md`
> and `docs/hermes_integration.md` are the two integration guides.

### Objection 2: "How is this different from Mem0?"

**Canned response:**

> Different scope. Mem0 is a memory store you drop into any agent.
> Nexus's memory layer is governance-aware (every write passes
> through a Covernor policy), bitemporal (valid_from/valid_to +
> observed_at/superseded_at), uses Beta-distributed confidence, and
> ships a skepticism layer that detects contradictions and marks
> `superseded_at` instead of silently overwriting. The
> hash-chained audit per user means you can cryptographically
> verify "what did the agent believe at time T" — see
> `GET /v1/memory/integrity`. Mem0 and Nexus solve adjacent
> problems; you could run both. Full architecture:
> `docs/memory.md`.

### Objection 3: "Does this actually block prompt injection, or is it just pattern matching?"

**Canned response:**

> It's defense-in-depth, not silver-bullet. Nexus runs a
> 11-language pattern scanner, Unicode normalization (zero-width,
> homoglyphs, fullwidth, combining diacritics), a semantic memory
> bank of previously-blocked attempts, and a per-session escalation
> tracker. We ship a 18-attack red-team benchmark
> (`tests/eval/tool_injection_redteam.py`) with exit gate 100%
> block rate on 10 attack categories, runs every commit. Is it
> breakable against a determined attacker with novel vectors? Yes —
> no scanner isn't. The point is that blocked attempts land in the
> labeling queue and become training examples for the next
> fine-tune. The benchmark is the receipt:
> `python -m tests.eval.tool_injection_redteam --json`.

### Objection 4: "Hash-chain is just hype — it's a linked list with SHA-256."

**Canned response:**

> Correct, and that's the point. It's a per-user per-belief linked
> list of hashes such that any byte modified anywhere in the chain
> changes the final hash. That's enough to catch tampering and
> enough to prove "these beliefs existed at this order" without
> inventing a blockchain. `contradiction_qa` benchmark has a
> single-byte tamper test that verifies this end-to-end; the
> `GET /v1/memory/integrity` endpoint and `nexus memory verify`
> CLI externalize the verification. Hash-chain is the cheapest
> correct solution to the audit problem — deliberately boring.
> `docs/memory.md §"Hash-chain integrity"` shows the exact hash
> inputs.

---

## Fallbacks

### HN post flagged or marked [dead]

- Do not post a second time within 24h.
- Email hn@ycombinator.com (one short, honest message — "my post
  was flagged, I don't believe I violated the guidelines, here's
  the post ID").
- Continue the launch on Reddit / X; the HN traffic is lost, the
  launch is not.

### HN post stuck at karma 1–3 for 60min

- This is normal during off-peak hours. Do nothing.
- Do NOT upvote from alt accounts. Trackable and fatal.
- If at T+120min still at karma < 5, quietly withdraw and plan to
  repost 24h later in a different TZ peak.

### Demo site returning 5xx

Refer to the Nexus production runbook (not this doc). Execute in
order:

1. Check `/health/ready?deep=true` — which dependency is red?
2. If Postgres: check pool exhaustion (`DB_POOL_SIZE`); scale up
   workers before scaling down. Dashboard has the counters.
3. If LLM provider: check circuit breakers at
   `/dashboard/circuit-breakers`. Manual reset any stuck-open
   breakers.
4. If traffic itself: scale gunicorn workers. `GUNICORN_WORKERS`
   in `.env`.
5. If none of the above: **turn off the public demo**. It's better
   to have no demo than a broken demo. Replace the URL with a
   "demo pending" banner and keep the repo live.

### A critical security bug is disclosed in a public comment

- Thank the reporter publicly, immediately.
- Confirm the bug privately (DM / email).
- File a PRIVATE GitHub Security Advisory. Do NOT fix in a public
  PR.
- Ship the fix within 24 hours. Cut a patch release.
- Post a public follow-up once patched, crediting the reporter.
  *This is a trust-building moment — handle it well and the launch
  is improved, not hurt.*

### A benchmark fails on someone else's machine

- Ask for the environment (Python version, OS, `.env`).
- Reproduce on your end if possible.
- If it's a real regression, revert the triggering commit, cut a
  patch release, post an update.
- If it's an env issue, update `docs/benchmarks.md` with the
  minimum supported env. Thank them for surfacing it.

### You catch yourself tweeting something defensive

- Close the tab.
- Re-read the philosophy section of this doc.
- The only winning move against a hostile comment is *a receipt*.
  Every snarky reply costs you one operator; every receipt-bearing
  reply gains you one.

---

## Exit criteria

The launch is "over" when **all** of the following are true. Not
when you're tired; not when HN traffic drops.

- [ ] HN post has reached its final karma (no change for 4h).
- [ ] Every issue opened during the launch window has a first-pass
  triage label and an acknowledgment from you.
- [ ] Every substantive HN / Reddit / X comment has a reply.
- [ ] `/health` has been 200 for a continuous 4h.
- [ ] Docker image `:latest` tag points at the launch commit
  (you did NOT push during the window).
- [ ] A one-paragraph internal post-mortem is written (what went
  well / what broke / what surprised you) — this doc's bottom
  section is where the template lives.

Typical duration: 36–48 hours post-launch. Block your calendar
accordingly.

---

## Post-launch: the 7-day operator checklist

Day 0 (launch day, covered above).

**Day 1 (L+24h):**

- [ ] Respond to every opened issue. Even "thanks, noted" is fine.
- [ ] Re-check the benchmark CI. Every night between Day 0 and
  Day 7 must be green; if any red, fix same-day.
- [ ] Post a brief "what I learned from the launch" update on X.
  One post. Not a thread. Not dramatic.

**Day 2–3:**

- [ ] Triage the issue queue properly. Label: `bug`, `enhancement`,
  `question`, `docs`, `benchmark`.
- [ ] For every `docs` issue: fix it that day. These are the
  cheapest fixes and they compound — the next reader doesn't hit
  the same confusion.

**Day 4–5:**

- [ ] Cut a patch release if any launch-phase bugs were fixed.
  Semver: `v1.0.N+1` is fine; no need for a `v1.1` yet.
- [ ] Update `CHANGELOG.md` explicitly calling out launch-phase
  contributions (by name, with thanks).

**Day 6–7:**

- [ ] Write the post-mortem (see template below).
- [ ] Archive the launch-drafts gist publicly (so future launchers
  have a reference).
- [ ] Decide: is Phase 13 gate reached? See
  [MEMORY_FLAGSHIP_PLAN.md §Phase 13 Gate](../MEMORY_FLAGSHIP_PLAN.md).
  If yes, begin Phase 13 planning; if not, extend Phase 12 polish
  by 4 weeks.

### Post-mortem template

Keep it under 500 words. Publish as a GitHub discussion or blog
post, whichever fits. Five sections:

1. **What we shipped.** One paragraph. Link the launch commit.
2. **What went well.** Three bullets. Receipts, not feelings.
3. **What broke.** Three bullets with the fix commit links.
4. **What surprised us.** Three bullets — especially user
   feedback that reframed how we think about the product.
5. **What's next.** Link to the Phase 13 plan if the gate is
   reached; otherwise link to the Phase 12 extension.

---

## Anti-patterns

Things that look like launch tactics but actively hurt. Do not do
any of these, even if you see other OSS projects do them.

| Anti-pattern | Why it hurts |
|---|---|
| Begging for stars ("please star if you like this") | Signals insecurity; devalues every genuine star |
| "We're the OpenAI of X" comparisons | HN flags immediately, the audience rolls eyes |
| Pinning the post to your own HN profile | HN detects and penalizes |
| Multi-account upvoting | Same — and permanent ban-risk for the account |
| Screenshot-heavy marketing assets | Signal that the code doesn't speak for itself |
| Changelog posts that read "revolutionary", "game-changing" | Terminology HN commenters will quote back at you |
| Any emoji in the HN title | HN strips them and the post looks broken |
| Starting a YouTube channel on launch day | Dilutes the one-shot attention budget |
| Promising a roadmap you can't deliver in 4 weeks | Every unmet promise costs you 10× its original goodwill |
| Apologizing for rough edges | Either fix them before launch or note them honestly in the README's "limits" section |
| Engaging with bad-faith replies more than twice | Every extra reply amplifies them; starve and move on |
| Ignoring a legit bug because "it's not representative" | The person who opened it is representative — they found it |

---

## See also

- [MEMORY_FLAGSHIP_PLAN.md §2.5](../MEMORY_FLAGSHIP_PLAN.md) —
  canonical positioning. The HN / Reddit / X copy must be
  consistent with this section; if it drifts, §2.5 wins.
- [docs/demo/screencast.md](demo/screencast.md) — the recording
  you'll link from the launch post.
- [docs/benchmarks.md](benchmarks.md) — every receipt referenced
  in the canned objection responses.
- [docs/openclaw_integration.md](openclaw_integration.md) +
  [docs/hermes_integration.md](hermes_integration.md) — the two
  integration docs newcomers will land on after the README.
- [README.md](../README.md) §"Killer demos" — the four-demo grid
  that replaces marketing copy.

---

_Last updated L-? — update the date header before every real
launch. This doc is internal-only; do not link it from the README
or public channels._
