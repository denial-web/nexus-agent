# 60-second injection demo GIF — recording script

**Goal:** One terminal recording for the README hero — immune scan blocking multilingual injection.  
**Output:** `docs/assets/injection_demo.gif` (or embed in README via GitHub raw URL).

---

## Fastest path (~30s recording)

No LLM calls. No API key. Works on a fresh clone after `pip install -r requirements.txt`.

```bash
cd nexus-agent
python examples/injection_demo.py --immune-only
```

Regenerate the README GIF (requires `brew install agg`):

```bash
make injection-demo-gif
# → docs/assets/injection_demo.gif
```

Record the terminal. Expected lines:

```text
[PASS] Safe (English)
[BLOCK] or [FLAG] Injection (English)
[FLAG]  Injection (Chinese)
[FLAG]  Injection (Russian)
...
[PASS] Safe (Technical)
```

> **Note:** `--immune-only` runs the in-process scanner. Full-pipeline demo (`python examples/injection_demo.py` with `make dev`) may show `BLOCK` vs `FLAG` differences on English due to session escalation — use full mode for HN comments if challenged.

---

## Full pipeline path (~2 min with mock LLM)

```bash
cp .env.example .env
# Leave provider keys empty for mock mode; leave NEXUS_API_KEY empty for dev auth-off
make dev
# second terminal:
python examples/injection_demo.py
```

If `NEXUS_API_KEY` is set in `.env`, the demo loads it automatically.

---

## Recording settings

- Terminal font **16pt+**, dark theme
- Window width ~90 cols (demo prints 70-char banners)
- Tool: macOS Screen Recording, asciinema, or ScreenFlow
- Trim to first complete run; no scrolling

---

## README embed (after export)

```markdown
![Injection demo](docs/assets/injection_demo.gif)
```

---

## See also

- [external_eval.md](../external_eval.md) — Tier A benchmark numbers
- [screencast.md](screencast.md) — full 10-minute product screencast
