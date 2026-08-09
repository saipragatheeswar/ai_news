# AI News Desk

A Django application that publishes about 20 original news articles a day about
whatever is genuinely trending, across world news, India, business, technology,
sports, entertainment, science and clearly-labelled unconfirmed reports.

It finds the day's hot topics with the Tavily search API, then writes each story
with a small local language model running under Ollama. Nothing is republished
verbatim, and every draft has to clear an originality check and an editorial
safety check before it can go live.

## How it works

```
Tavily search  ->  cluster into topics  ->  rank by heat  ->  gather sources
                                                                    |
                                                                    v
                                                        extract facts (model)
                                                                    |
                                                                    v
                                                          write article (model)
                                                                    |
                                        +---------------------------+
                                        v
                            length -> originality -> safety rules -> model review
                                        |                |               |
                                     rewrite          rewrite            |
                                                                         v
                                                    publish / hold for review / reject
```

### Finding hot topics

Seed queries per category are sent to Tavily's news search. Results are grouped
into clusters using keyword overlap between headlines, so the same story
reported by six outlets becomes one topic rather than six.

Each cluster gets a heat score built mainly from **how many independent domains
carry it**, plus average search relevance and a recency bonus. Topics are then
selected round-robin across categories, so a busy sports day cannot swallow the
whole front page. A topic fingerprint (sorted, stop-worded keywords, hashed)
prevents us from covering the same story twice within seven days.

### Writing without copying

Copyright protects the *expression* of facts, not the facts themselves. The
rewriter is built around that distinction and works in two stages:

1. **Extract.** The model reads the source coverage and returns a plain list of
   bare facts, with the original phrasing discarded. Direct quotations become
   paraphrases.
2. **Write.** The model composes the article from *only* that fact list. It
   never sees the source prose while writing, so there is nothing to copy.

Every draft is then measured against the sources it came from:

| Check | Default limit | Why |
| --- | --- | --- |
| 5-gram overlap | 12% | Catches sentence-level paraphrase that only swaps a few words |
| Longest verbatim run | 9 words | Catches a single copied sentence that overall overlap would hide |

Fail either and the draft is discarded and rewritten from scratch, with the
failure fed back into the prompt. In practice independent writing lands around
5-6% overlap.

### Editorial safety

Two independent checks run on every draft. Deterministic rules are the only
thing trusted to **block**; the model's review can only downgrade an article to
human review, because a 2 GB model is not reliable enough to be the sole gate.

Blocked outright:

- Sexual, adult or explicit content, and anything matching `data/blocklist.txt`
- **Accusations without attribution** — if a sentence says someone is a
  fraudster, is corrupt, stole, is guilty and so on, the same sentence must
  attribute it to a court, investigation, official or named source. This is the
  main defamation guard.
- Rumours written as established fact, or rumours carried by fewer than two
  independent outlets
- Personal data: emails, phone numbers, ID numbers, card numbers
- Prohibited categories: CSAM references, weapon or self-harm instructions

Held for human review:

- Anything the model reviewer flags
- First-person voice, or an article below the minimum length

Everything held or rejected stays in the database so decisions can be audited.
Held articles are visible to staff at their normal URL with a warning banner,
and invisible to the public.

## Setup

Requires Python 3.11+ and [Ollama](https://ollama.com).

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# Pull a model. Any of these fit comfortably under 4 GB:
ollama pull llama3.2       # ~2.0 GB, the default
# ollama pull qwen2.5:3b   # ~1.9 GB, often better at following instructions
# ollama pull gemma2:2b    # ~1.6 GB, fastest

Copy-Item .env.example .env   # then add your TAVILY_API_KEY

.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py createsuperuser
```

Check that both external services are reachable before your first run:

```powershell
.\.venv\Scripts\python.exe manage.py check_setup
```

## Running it

```powershell
# Publish today's articles (this is the command to schedule)
.\.venv\Scripts\python.exe manage.py run_daily

# Same, but send everything to the review queue instead of publishing
.\.venv\Scripts\python.exe manage.py run_daily --hold-all

# Serve the site
.\.venv\Scripts\python.exe manage.py runserver
```

Then open <http://127.0.0.1:8000/> for the site and
<http://127.0.0.1:8000/admin/> for the editorial desk.

### Other commands

| Command | What it does |
| --- | --- |
| `check_setup` | Verifies the Tavily key and the local model both work |
| `discover_topics` | Finds and ranks today's topics without writing anything |
| `demo_write` | Writes one article from built-in fixture sources, no API calls |
| `run_daily` | The full daily run |
| `run_scheduler` | Blocking process that triggers `run_daily` at a set time |

`demo_write` is the fastest way to judge whether a model is good enough:

```powershell
.\.venv\Scripts\python.exe manage.py demo_write --fixture technology
```

## Scheduling the daily run

Either run the bundled scheduler as an always-on process:

```powershell
.\.venv\Scripts\python.exe manage.py run_scheduler        # uses SCHEDULE_HOUR/MINUTE
```

Or, better on Windows, let Task Scheduler own the timing:

```powershell
schtasks /create /tn "AI News Daily" /tr "cmd /c cd /d C:\saipragatheeswar\ai_news && .venv\Scripts\python.exe manage.py run_daily" /sc daily /st 06:30
```

A run takes roughly 1-3 minutes per article on CPU, so budget 30-60 minutes for
20 articles. Runs are recorded in `PipelineRun` and visible at `/status/`.

## Editorial workflow

The Django admin is the newsroom. Under **Articles** you can filter by status to
find the review queue, read the originality score and safety flags on each one,
edit the copy, and then use the actions to publish, reject, or send back to
review. **Topics** shows what was discovered and why anything was skipped.

Set `AUTO_PUBLISH=0` in `.env` if you want a human to approve every story
before it goes live. This is the safer setting, and the one to use if you are
publishing under your own name or brand.

## Configuration

All tuning lives in `.env`; see `.env.example` for the full annotated list. The
settings you are most likely to change:

| Setting | Default | Effect |
| --- | --- | --- |
| `DAILY_ARTICLE_TARGET` | 20 | Articles to publish per run |
| `OLLAMA_MODEL` | `llama3.2:latest` | Which local model writes |
| `AUTO_PUBLISH` | 1 | Set to 0 to hold everything for review |
| `MAX_NGRAM_OVERLAP` | 0.12 | Lower is stricter on originality |
| `MAX_LONGEST_COMMON_RUN` | 9 | Lower is stricter on verbatim runs |
| `MIN_SOURCES_FOR_RUMOUR` | 2 | Outlets needed before a rumour is written |
| `MIN_ARTICLE_WORDS` | 130 | Below this an article is held for review |
| `MAX_REWRITE_ATTEMPTS` | 3 | Rewrites allowed before giving up on a topic |

Categories and their seed queries are in `NEWS_CATEGORY_QUERIES` in
`config/settings.py`. Add a beat by adding a key and a couple of queries.

## Tests

```powershell
.\.venv\Scripts\python.exe manage.py test news
```

The suite covers the parts that must not silently break: the originality
thresholds, every safety rule, headline clustering, topic fingerprinting, and
that unpublished articles stay invisible to the public.

## Before you publish publicly

This is a working system, but a few things are on you:

- **Read the drafts.** A 2 GB model gets facts subtly wrong. Start with
  `AUTO_PUBLISH=0` and read output for a week before trusting it.
- **The safety rules are a floor, not a ceiling.** They catch the obvious
  failure modes. They cannot tell you whether a story is fair.
- **Keep the attribution links.** They are what makes this a summary that
  points readers to the original reporting rather than a substitute for it.
- **Set `DJANGO_DEBUG=0`**, a real `DJANGO_SECRET_KEY` and proper
  `DJANGO_ALLOWED_HOSTS`, and serve behind HTTPS with a real web server.
  SQLite is fine at this volume, but move to PostgreSQL if you add traffic.
- **Respect the sources.** Check `robots.txt` and terms for outlets you lean on
  heavily, and honour takedown requests quickly.
