# Launch pre-flight checklist

**Run this before warm-up posts or Show HN.** Last verified: 2026-07-01 (fresh clone `/tmp`, `python examples/injection_demo.py --immune-only` PASS).

---

## T-7 days (credibility)

- [x] README injection GIF (`docs/assets/injection_demo.gif`)
- [x] Tier A eval page (`docs/external_eval.md` + evidence snapshot)
- [x] `LICENSE` · `SECURITY.md` · `CONTRIBUTING.md` on default branch
- [ ] Test count in launch copy matches README badge (currently **1400**)
- [ ] `LAUNCH_POSTS.md` reviewed — no "beat GPT" claims

---

## T-3 days (clone-and-wow)

Run on a **clean machine** or `/tmp` scratch dir:

```bash
git clone https://github.com/denial-web/nexus-agent.git
cd nexus-agent
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python examples/injection_demo.py --immune-only   # must finish <5s, PASS/BLOCK/FLAG visible
```

Optional full pipeline (mock LLM when provider keys empty):

```bash
cp .env.example .env
# leave NEXUS_API_KEY empty for dev auth-off; leave provider keys empty for mock
make dev
python examples/injection_demo.py
```

- [ ] Immune-only demo passes
- [ ] README GIF loads on GitHub
- [ ] 3–5 people asked to try clone path (track in issue or DM)

---

## T-1 day (warm-up posted)

- [ ] r/LocalLLaMA soft post live — see [LAUNCH_POSTS.md § Week 3 warm-up](../LAUNCH_POSTS.md#week-3-warm-up--rlocalllama-post-before-hn)
- [ ] X post with GIF + one sentence (optional)
- [ ] Issues from warm-up triaged or fixed

---

## Launch day (Show HN)

**When:** Tuesday–Thursday, 8–10am US Eastern

| Step | Action |
|------|--------|
| 1 | Post Show HN — `LAUNCH_POSTS.md` § Hacker News |
| 2 | Monitor GitHub issues + HN comments (respond <2h) |
| 3 | Same day: X thread |
| 4 | +1–2 days: cross-post Reddit if HN traction |

**Links to have ready:**

- Repo: https://github.com/denial-web/nexus-agent
- External eval: https://github.com/denial-web/nexus-agent/blob/master/docs/external_eval.md
- Benchmarks: https://github.com/denial-web/nexus-agent/blob/master/docs/benchmarks.md
- HF demo: https://huggingface.co/spaces/denialkhmbot/nexus-agent-demo

**Forbidden on launch day:** "beat GPT", full AgentDojo leaderboard, OWASP certification.

---

## Regenerate assets

```bash
make injection-demo-gif    # requires brew install agg
```

---

## See also

- [LAUNCH_POSTS.md](../LAUNCH_POSTS.md) — copy-paste posts
- [docs/fame_playbook.md](fame_playbook.md) — 48h incident-response runbook (internal)
- [docs/external_eval.md](external_eval.md) — benchmark honesty
