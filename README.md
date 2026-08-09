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

### Getting enough material

Search snippets are only a couple of hundred words, which is nowhere near enough
to write a substantial article from; early runs produced 100-word stubs for
exactly this reason. Every source URL is therefore passed back through Tavily's
extract endpoint to retrieve the full article text, giving the fact extractor
roughly twenty times more to work with.

Length is then a writing problem rather than a material one. A 2 GB model asked
for 600 words in one response returns a padded 200-word summary, so the article
is composed in three passes - opening, detail, outlook - each shown the copy
written so far and told not to repeat it. Near-duplicate paragraphs are dropped
during assembly.

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
| `check_setup` | Verifies the Tavily key, the local model and image search |
| `discover_topics` | Finds and ranks today's topics without writing anything |
| `demo_write` | Writes one article from built-in fixture sources, no API calls |
| `run_daily` | The full daily run |
| `purge_stale` | Deletes unread articles and their images past the retention window |
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

A 600-word article needs nine model calls, so budget 4-6 minutes per article on
a machine with spare cores and 12-15 minutes on a small CPU-only VPS. Runs are
recorded in `PipelineRun` and visible at `/status/` and `/desk/`.

Two things dominate that budget, and both are easy to get wrong:

- **Never vary `OLLAMA_NUM_CTX` between calls.** Ollama reloads the model when
  the context size changes, which costs 30-60 seconds every single time.
- **Keep the model resident.** The pipeline sends `OLLAMA_KEEP_ALIVE` (default
  `30m`) with each request so it stays loaded across a run. Leave the server's
  own idle default low; the per-request value overrides it while a run is
  active, and the memory is released afterwards.

Pair the daily run with the purge so storage stays flat:

```bash
30 4 * * *  cd /var/www/ai_news && ./.venv/bin/python manage.py run_daily
30 9 * * *  cd /var/www/ai_news && ./.venv/bin/python manage.py purge_stale
```

## Images

Articles are illustrated with **openly-licensed** images only, fetched from
Openverse and Wikimedia Commons, downscaled, stripped of metadata and stored
under `MEDIA_ROOT` on your own server. The licence and creator are recorded on
every file and shown in the caption.

The pipeline deliberately does **not** copy pictures from the outlets it
summarises. Their photography is normally licensed from agencies (Reuters, AP,
PTI, Getty), so the outlet has no right to sub-licence it, and image claims are
the most aggressively pursued form of online copyright enforcement. Because an
openly-licensed photo illustrates the subject rather than documenting the event,
captions say so explicitly.

Stories with no suitable image get a generated category card in CSS, so the
layout never breaks. Set `FETCH_IMAGES=0` to use cards everywhere.

In production nginx must serve the media directory:

```nginx
location /media/ {
    alias /var/www/ai_news/media/;
    expires 30d;
    access_log off;
}
```

## Retention

Storage is capped by readership rather than by age alone. `purge_stale` deletes
articles older than `RETENTION_DAYS` (7) that attracted fewer than
`RETENTION_MIN_VIEWS` (5) views, together with their image files and any topic
rows whose cached source text is no longer referenced. Stories people actually
read are kept. Anything still awaiting review is never deleted.

Views are counted on the article page, excluding staff previews and obvious
crawlers, and shown on `/desk/`.

```bash
python manage.py purge_stale --dry-run   # see what would go
```

## Editorial workflow

`/desk/` is the newsroom, and is visible to staff accounts only. It lists
everything produced, grouped by day, with the status, word count, view count,
originality score and safety flags for each story, and buttons to publish,
unpublish, edit or delete it outright. Deleting removes the stored image too.

The Django admin at `/admin/` remains available for bulk work: filter **Articles**
by status to work the review queue, edit copy inline, and use the actions to
publish, reject or send back to review. **Topics** shows what was discovered and
why anything was skipped.

Set `AUTO_PUBLISH=0` in `.env` if you want a human to approve every story
before it goes live. This is the safer setting, and the one to use if you are
publishing under your own name or brand.

## Configuration

All tuning lives in `.env`; see `.env.example` for the full annotated list. The
settings you are most likely to change:

| Setting | Default | Effect |
| --- | --- | --- |
| `DAILY_ARTICLE_TARGET` | 10 | Articles to publish per run |
| `OLLAMA_MODEL` | `llama3.2:latest` | Which local model writes |
| `AUTO_PUBLISH` | 1 | Set to 0 to hold everything for review |
| `MAX_NGRAM_OVERLAP` | 0.12 | Lower is stricter on originality |
| `MAX_LONGEST_COMMON_RUN` | 9 | Lower is stricter on verbatim runs |
| `MIN_SOURCES_FOR_RUMOUR` | 2 | Outlets needed before a rumour is written |
| `MIN_ARTICLE_WORDS` | 420 | Below this an article is held for review |
| `TARGET_ARTICLE_WORDS` | 600 | Length the section prompts aim for |
| `SOURCE_TEXT_BUDGET` | 24000 | Characters of source text fed to the extractor |
| `OLLAMA_KEEP_ALIVE` | `30m` | How long the model stays resident between calls |
| `FETCH_IMAGES` | 1 | Set to 0 for generated category cards only |
| `RETENTION_DAYS` | 7 | Age at which unread stories become purgeable |
| `RETENTION_MIN_VIEWS` | 5 | Views that save a story from being purged |
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
