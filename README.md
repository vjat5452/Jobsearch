# JobFlow

Finds jobs on LinkedIn, Naukri, Indeed, Glassdoor, Unstop and company career boards, ranks every one
against your resume, and applies for you: **automatically** for quick-apply jobs that clearly fit,
**with your approval** for everything else.

```
 discover ──► filter ──► store (diff) ──► score (batch LLM + budget) ──► rules ──► route
 boards (HTTP)   title/stack/   new/closed/      25 jobs/request          seniority     auto ──► worker applies
 sites (Chrome)  exp/salary/    reopened,        positional replies       relevance     review ─► dashboard: you approve
                 scam phrases   cross-site dedupe                                       skip
```

## Quick start

```powershell
cd C:\j_f
pip install -r requirements.txt                      # once
copy config\settings.example.yaml config\settings.yaml   # once, then edit keywords/locations
python main.py login      # once per site: log in in the window that opens, then press Enter
python main.py web        # dashboard at http://127.0.0.1:8765  (Setup tab: key, resume, profile)
python main.py start      # worker in the background: discover, score, apply
python main.py status     # is it running? per-site counts and the last log lines
python main.py stop       # stop the worker and its Chrome
```

Before the first real run, put your Groq key in `secrets.txt` (`GROQ_API_KEY=gsk_...`), set `resume_path`,
and check `config/profile.yaml`. Try `python main.py apply --only linkedin --dry-run --limit 2` first: it fills
forms but never submits. Full setup details are below.

## The hybrid

| Route | When | What happens |
|---|---|---|
| **auto** | an in-site quick apply (LinkedIn Easy Apply, Naukri Apply, Indeed Easily apply, Glassdoor Easy Apply, Unstop) scoring ≥ `auto_min` (7/10), low scam risk | the worker applies on its own |
| **review** | company application forms, "apply on company site" links, quick-apply jobs scoring 6, medium scam risk | waits in the dashboard: **Approve** (the bot applies), **Open**, **I applied**, **Skip** |
| **skip** | below `review_min`, filtered, scam phrase, high scam risk, or a duplicate | recorded with the reason |

Thresholds are per site in `config/settings.yaml` → `routing`.

**One role, one application.** The same job on LinkedIn, Naukri and the company site collapses to one
fingerprint (company + title, with suffixes like "Pvt Ltd" and "(Remote)" stripped). The copy on the site
earliest in `routing.prefer` wins.

**The model's score is checked in code.** A Senior/Staff/Lead title caps at 3/10 (unless your profile
has 6+ years), and a job weak on both skills and domain caps at 3/10. A model that ignores the prompt still
cannot make the bot apply to a role you can't get. A job auto-routed on a search-card snippet is re-scored
once the full posting loads.

**It speaks only from your facts.** Answers come from `config/profile.yaml`, from answers you gave before,
or from the model restricted to profile facts. A skill you didn't list is answered as 0 years. When a
question needs a fact you haven't given, the job waits and the question appears in the dashboard.
Answer it once and every job blocked on it goes back in the queue.

**Everything sent is on record.** The **Applied** tab shows each application with every answer given and
a screenshot of the final page.

## Safety and limits

- It drives **your installed Chrome** with its own profile. You log in to each site yourself, once. The
  bot never types passwords.
- It **never solves CAPTCHAs.** On a verification check it pauses that site for 3 hours and notifies you.
  On a company form's CAPTCHA it brings the window on screen and waits for you.
- Daily caps are on by default (LinkedIn 25, Naukri 40, Indeed 20, Glassdoor/Unstop/company forms 15),
  with 60–180 s between applications.
- Automated applying is against the terms of service of LinkedIn, Indeed and Naukri. Accounts that apply
  at bot-like rates can be restricted. Keep the caps modest. You use this at your own risk.

## Setup (Windows 10/11)

Needs **Python 3.12+** (python.org, tick "Add to PATH") and **Google Chrome**.

```powershell
cd C:\j_f
pip install -r requirements.txt
copy config\settings.example.yaml config\settings.yaml
```

1. **Key.** Get a free key at https://console.groq.com/keys and put it in `secrets.txt`:
   `GROQ_API_KEY=gsk_...` (gitignored). Any OpenAI-compatible provider works; see `llm` in settings.
2. **Resume.** Put your PDF in the folder and set `resume_path` in `config/settings.yaml`.
3. **Profile.** `python main.py init` drafts `config/profile.yaml` from your resume. **Check every line
   marked `# CONFIRM`.** Everything the bot writes on an application comes from this file.
4. **Search.** Edit keywords, locations and `companies:` in `config/settings.yaml`.
5. **Log in.** `python main.py login` shows the bot's Chrome with each site's login page. Log in, then
   press Enter.
6. **Try it without submitting.**
   ```powershell
   python main.py discover --only ats      # company boards, no browser needed
   python main.py score                    # rank + route
   python main.py discover --only linkedin
   python main.py score
   python main.py apply --only linkedin --dry-run --limit 2   # fills forms, never submits
   python main.py web                      # http://127.0.0.1:8765 - check Applied for what it would have sent
   ```
7. **Go.** `python main.py start` runs the worker hidden in the background. Optional:
   `powershell -ExecutionPolicy Bypass -File .\scripts\install_autostart.ps1` starts it at every Windows
   login, and `scripts\lock_folder.ps1` restricts the folder to your account.

## Everyday

| Command | |
|---|---|
| `python main.py web` | dashboard: Approve · Questions · Applied · Browse · Sites, live activity |
| `python main.py status` | running? per-site counts, pauses, last log lines |
| `python main.py stop` / `start` | restart after changing settings |
| `python main.py login` | when a site shows "not logged in" |
| `python main.py questions` | answer pending questions in the terminal instead |
| `python main.py report` | jobs by site/status, recent applications |
| `python main.py recap` | re-apply the scoring rules and re-route, no tokens |
| `python main.py run --only naukri` | one manual discover + score + apply pass |

The worker pauses a site by itself: until tomorrow on a daily limit or cap, 3 h on a verification check,
1 h after 3 failures in a row (screenshots in `data\screens`), 30 min when logged out. Any crash restarts
it after a minute. With `email.enabled`, a summary arrives each evening.

## Tests

```powershell
python tests\run_all.py
```

No network, browser or key is needed. Covered: the diff and closures, cross-site duplicates, every
routing rule, positional parsing and salvage, the budget under 5× cost, a model that ignores the seniority
rule, apply outcomes (applied, needs answer, already applied, external, closed, limit, verification check,
logged out, 3 failures, cap, dry run, scam phrase on the full posting), the answer bank and filters.

## Layout

```
main.py                 CLI
jobflow/config.py       settings, profile, secrets.txt
jobflow/models.py       the job shape, ids, cross-site fingerprint
jobflow/store.py        SQLite (WAL): jobs, questions, applications, events, site state, spend
jobflow/boards.py       Greenhouse, Lever, Ashby, Workable, Workday, Oracle, Atlassian, Remotive, Arbeitnow
jobflow/sites/          linkedin, naukri, indeed, glassdoor, unstop, smartapply, ats_form (company forms)
jobflow/filters.py      title / stack / location / experience / salary / scam phrases
jobflow/llm.py          OpenAI-compatible + Anthropic client, form answers, cover notes, profile draft
jobflow/scoring.py      batch scoring, parser, daily budget, recap
jobflow/gates.py        seniority and relevance caps
jobflow/router.py       auto / review / skip + duplicates
jobflow/forms.py        generic form filler
jobflow/answers.py      answer bank -> model -> ask you
jobflow/pipeline.py     discover, score+route, apply round
jobflow/worker.py       the non-stop loop and pause rules
jobflow/notify.py       Windows notification, email summary
jobflow/web/            FastAPI dashboard
```
