# Discord Haiku chat bot

A small, text-only Discord bot for Railway. It uses **Claude Haiku 5.5 at low effort with thinking disabled**, keeps replies short, and adapts its writing style using local statistics. By default, it saves new human text messages in allowed channels and learns their writing style locally, even when nobody mentions it. It only calls Claude when someone addresses it, unless you explicitly enable an automatic-reply channel.

Defaults: **one chat request per person every 15 seconds**, **120 output tokens**, **45 words**, **300 characters**, and shared allowances of **$0.50/day, $5/month, $5 total**. The API output limit has an absolute ceiling of **20,000 tokens**, even when changing configuration. Leave the default at 120 for ordinary conversation.

The bot cannot run commands, browse links, use tools, or change its settings through a conversation. No language-model prompt can guarantee perfect jailbreak resistance; capabilities, budgets, rate limits, and final reply size are enforced by application code. Adapting style and remembering selected facts does not retrain the model or increase its underlying intelligence.

## Get it online

### 1. Create the Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications), create an application, and open **Bot**.
2. Create/reset the bot token and keep it for Railway's `DISCORD_TOKEN` variable. Enable **Message Content Intent** under Privileged Gateway Intents. The bot needs this to read allowed-channel conversation and commands. Larger verified bots may require approval. [Discord intent documentation](https://docs.discord.com/developers/events/gateway#message-content-intent)
3. Install the application into your server with the **bot** scope. Grant **View Channel**, **Send Messages**, and **Read Message History** in the intended ordinary text channel. Read Message History lets the bot verify an older reply target if Discord omits it from the event. Administrator permissions are unnecessary. If using a thread, also grant **Send Messages in Threads** and allow that thread's own ID.
4. In Discord, enable **User Settings → Advanced → Developer Mode**. Right-click the intended channel and choose **Copy Channel ID**. Copy your own user ID the same way if you want the owner-only `!budget` command.
5. Obtain an API key with credits and access to Haiku 5.5 from the [Claude Console](https://platform.claude.com/).

### 2. Deploy on Railway

1. Create a Railway service from [w7llywonka/claudeagent](https://github.com/w7llywonka/claudeagent). The repository root is the service root. Do not upload your `.env`, tokens, or database.
2. Use the included **Dockerfile**. Leave the custom start command empty so Docker runs the entrypoint and `python -m bot`.
3. Add a **persistent volume mounted at `/data`**. Add the following Railway service variables:

   | Variable | Value |
   | --- | --- |
   | `DISCORD_TOKEN` | Your Discord bot token |
   | `ANTHROPIC_API_KEY` | Your Claude API key |
   | `ALLOWED_CHANNEL_IDS` | Your channel ID; comma-separate additional IDs |
   | `DB_PATH` | `/data/chat.db` |
   | `OWNER_IDS` | Your user ID, optional |

4. Keep **one replica**, turn **Serverless off**, and leave **public networking and HTTP healthchecks unconfigured**. This is an outgoing Discord Gateway connection, with no website to expose. Set restart policy to **Always** on a paid plan, or **On Failure** on a trial. [Restart policies](https://docs.railway.com/deployments/restart-policy)
5. Deploy and check that the Railway logs show the bot connected. In an allowed channel, mention the bot and say hello. A successful reply is the live credential/API check; automated tests do not validate your tokens or spend API credits.

The container briefly starts its entrypoint as root to prepare the exact database paths, then drops to the unprivileged `app` user before starting Python. The volume preserves the message archive, memory, opt-outs, cooldowns, and spending records over restarts in `/data/chat.db`. A **256 MB volume is sufficient for the default archive limits**. The text-size cap does not include SQLite indexes, its write-ahead log, or other bot records, so leave storage headroom and increase the volume if you raise the limits. Railway volumes mount at runtime, cannot be shared across replicas, and may introduce brief deployment downtime. [Volume setup](https://docs.railway.com/volumes), [volume limitations](https://docs.railway.com/volumes/reference)

Setup uses Railway's dashboard deliberately: its older `railway.json`/`railway.toml` Config as Code is deprecated, with legacy support documented through December 1, 2026. [Railway configuration documentation](https://docs.railway.com/config-as-code/reference)

### 3. Choose when it talks

- **Default:** mention the bot **or use Discord's Reply action on one of its messages**. The reply's mention/ping toggle can be off. Reply detection checks the Discord event, local cache, and up to 4,096 persisted bot-message IDs, which survive restarts. If needed, it fetches only the exact message being replied to and checks its author; it never scans channel history. The fallback requires **Read Message History** and is limited to 20 lookups per minute, two concurrent lookups, and a five-second timeout. No Claude tokens are used for this check.
- **Dedicated chat channel:** set `AUTO_REPLY_CHANNEL_IDS` to a subset of `ALLOWED_CHANNEL_IDS`. Every human message there is eligible for a reply, subject to the same limits.
- **Direct messages:** set `ENABLE_DMS=true` to allow them. They are disabled by default.

An invalid `ALLOWED_CHANNEL_IDS` prevents startup. An empty list permits no server channels and prevents startup unless DMs are explicitly enabled. Channels outside the allowlist are ignored. Other bots and webhooks do not trigger conversations. A person's chat cooldown applies across channels and survives restarts; it is not a separate allowance in every channel.

Change `COOLDOWN_SECONDS` in Railway variables and redeploy to adjust it whenever you want. The default is `15`; `0` means **no per-user cooldown**. Whole-number values from `0` to `86400` are accepted. Shared rate limits, spending limits and one in-flight reply per user/channel still apply, including at zero cooldown. These reply limits do not stop passive archiving or local style updates: eligible messages are still observed during a cooldown or when the API allowance is exhausted.

## Commands

These are local commands and do not call Claude.

| Command | What it does |
| --- | --- |
| `!help` | Show a short usage guide. |
| `!privacy` | Explain storage and privacy controls. |
| `!remember <fact>` | Save a short fact about yourself for this channel. |
| `!forget` | Clear your archived messages, saved facts, style data and recent local entries, plus cached bot replies. |
| `!optout` | Clear your data and stop storing new conversation/style data. Your future replies use only the current message. |
| `!optin` | Resume local learning and memory. |
| `!budget` | Show the local spending ledger; only users listed in `OWNER_IDS` can use it. |

Forgetting local data does not delete messages already posted to Discord or requests already received by Anthropic. Minimal opt-out, rate-limit and spending records remain as needed to enforce those controls. Content-free bot-message IDs also remain so replies still work; they contain only the bot/channel/message identifiers and timestamp. API keys are never placed in prompts. Keep them in Railway variables or your local process environment, never in chat or GitHub.

## Passive message archive and style learning

With `PASSIVE_LEARNING=true`, the bot archives **every new eligible human text message it receives in allowed channels**, including unmentioned messages and messages sent during reply cooldowns, API outages or after the API budget runs out. Bots, webhooks, disallowed channels and opted-out users are excluded. `!forget` and `!optout` are never retained. Other commands and obvious instruction-override attempts are archived for eligible users but excluded from style learning and recent prompt context. Stored text is sanitized and capped at 4,000 characters per message; Discord message IDs prevent duplicate recording. Attachments are not downloaded, linked pages are not read, and old Discord history is not backfilled.

The archive stays local in SQLite on the Railway volume. Its default limits are **30 days, 50,000 messages, and 64 MiB of stored message text** across the bot. The oldest messages are removed when a row-count or text-size limit is reached; expired rows are cleaned on startup and roughly hourly. The text-size limit counts UTF-8 bytes, not the whole database file. Set `MESSAGE_RETENTION_DAYS` (1–365), `MESSAGE_MAX_ROWS` (100–1,000,000), and `MESSAGE_MAX_MB` (1–128) to change these limits. `!forget` and `!optout` still clear the user's archived content.

Each ordinary eligible message incrementally updates small numeric style statistics for its author. Commands and obvious instruction-override attempts do not affect those statistics. A channel-wide style summary averages the stored user profiles. This uses no paid AI calls for ambient conversation and does not reread the raw archive to learn on each reply. It adapts writing habits; it does not retrain Claude or update model weights. Set `PASSIVE_LEARNING=false` to restrict archiving and style updates to triggered conversations instead of ambient channel chat.

## How it stays inexpensive

Only a compact style summary and a small recent conversation window are included in each Claude request. The local message archive is **not** sent wholesale: by default the bot sends at most six recent context entries, with a maximum of eight, and recent context expires after 30 minutes. Saving more messages does not make every future request longer. No embedding service, vector database, or AI summarizer runs in the background.

Explicit memory is bounded to eight facts per user per channel, at most 160 characters each, expiring after 30 days. Local keyword matching ranks up to three facts, with recent facts filling any unmatched slots. Facts are only retrieved for their owner in that channel. Style statistics also expire after 30 days without updates. Expired data is excluded from prompts immediately; physical cleanup runs periodically during activity. Recent channel conversation can contain other participating users' messages; saved personal facts are kept separate. Opted-out users' requests omit both ambient context and saved memory.

Requests are capped by input size, output size, two concurrent calls and a global 20-request/minute default. The bot does not automatically retry Claude requests. It neither downloads attachments nor reads linked pages. Outgoing text has Discord mentions disabled and is shortened before posting.

Prompt caching is **off by default**. Haiku 5.5 requires a cacheable prefix of at least 512 tokens; this bot's short fixed policy may be below that threshold. Setting `CACHE_SYSTEM_PROMPT=true` only marks the static policy for caching. It does not pad the prompt or cache changing channel data. Enable it only after measuring cache hits on a policy that naturally meets the minimum. Cache misses cost more than ordinary input when a cache write occurs. [Anthropic caching documentation](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)

## Cost and spending controls

Prices checked **October 12, 2026**: Haiku 5.5 prompts up to 100,000 tokens cost **$0.10 per million input tokens** and **$0.50 per million output tokens**. This bot's bounded requests stay far below that tier boundary. [Official model pricing](https://platform.claude.com/docs/en/models/haiku-5-5/overview)

Example: 1,000 input tokens + 80 output tokens costs about **$0.00014**, or roughly **7,143 replies per $1 of Claude API credit**. That is an illustration, not a guaranteed reply count. Actual prompt length, replies, cache writes and failed requests affect the result. **Railway hosting is separate.**

All users share the configured daily, monthly and lifetime allowances. Daily and monthly periods use UTC. Before a paid call, the bot reserves a conservative amount in SQLite; successful responses reconcile against reported usage. After a failed or interrupted call, an uncertain reservation remains counted so a restart cannot silently refund a possibly billed request. This can stop the bot earlier than actual provider spend. The ledger is an application safeguard, not an Anthropic balance or invoice.

Set an appropriate spending limit in your Anthropic account as well. Accounting depends on the prices in code matching the provider's current rates, and only covers this bot's database. Other applications using the key are outside its ledger. Deleting/replacing the database also removes its cumulative spending history. To authorize another allowance later, raise the cumulative `TOTAL_BUDGET_USD` limit deliberately rather than deleting the ledger.

## Configuration reference

Use `.env.example` as a complete variable reference. Booleans use `true` or `false`; IDs are comma-separated numeric Discord IDs. Model, low effort and disabled thinking are fixed in code. [Anthropic effort support](https://platform.claude.com/docs/en/build-with-claude/effort)

| Variable | Default | Purpose |
| --- | --- | --- |
| `DAILY_BUDGET_USD` | `0.50` | Shared daily API allowance |
| `MONTHLY_BUDGET_USD` | `5` | Shared monthly API allowance |
| `TOTAL_BUDGET_USD` | `5` | Lifetime allowance for this database |
| `MAX_OUTPUT_TOKENS` | `120` | API reply limit; absolute maximum 20,000 |
| `MAX_REPLY_WORDS` | `45` | Maximum words posted |
| `MAX_REPLY_CHARS` | `300` | Maximum characters posted |
| `MAX_INPUT_CHARS` | `800` | Maximum current message text sent to Claude; separate from archive storage |
| `CONTEXT_MESSAGES` | `6` | Number of recent entries included |
| `MAX_CONCURRENT_REQUESTS` | `2` | Maximum simultaneous Claude requests |
| `MAX_REQUESTS_PER_MINUTE` | `20` | Shared request rate limit |
| `COOLDOWN_SECONDS` | `15` | Per-user cooldown across channels, 0 disables it |
| `PASSIVE_LEARNING` | `true` | Archive and learn style from ambient messages; false keeps triggered conversations only |
| `MESSAGE_RETENTION_DAYS` | `30` | Archive age limit, 1–365 days |
| `MESSAGE_MAX_ROWS` | `50000` | Maximum archived messages across the bot, 100–1,000,000 |
| `MESSAGE_MAX_MB` | `64` | Maximum archived text in MiB of UTF-8 bytes, 1–128; excludes SQLite overhead |
| `CACHE_SYSTEM_PROMPT` | `false` | Optional static policy cache marker |
| `ENABLE_DMS` | `false` | Allow direct messages |
| `AUTO_REPLY_CHANNEL_IDS` | empty | Allowed channels that respond without mentions |
| `OWNER_IDS` | empty | Users allowed to inspect `!budget` |
| `DB_PATH` | `data/chat.db` | Local path; use `/data/chat.db` in Docker/Railway |

## Run or test locally

Python **3.13** is the target runtime. Install dependencies and run the offline tests from this folder:

```console
python -m venv .venv
```

Activate `.venv` using your shell's normal command, then:

```console
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

For a live run, set the required environment variables in that terminal and run:

```console
python -m bot
```

Plain Python does **not** automatically load `.env`. You can instead copy `.env.example` to a private `.env`, fill it in, change `DB_PATH` to `/data/chat.db`, and use Docker:

```console
docker build -t discord-haiku-bot .
docker volume create discord-haiku-data
docker run --rm --env-file .env -v discord-haiku-data:/data discord-haiku-bot
```

Use one live instance per bot token/database. Stop the local instance before starting the Railway instance.

Direct dependencies are pinned to stable PyPI releases verified on October 12, 2026: [discord.py 2.7.1](https://pypi.org/project/discord.py/2.7.1/) and [httpx 0.28.1](https://pypi.org/project/httpx/0.28.1/). Tests use Python's built-in unittest; no development dependencies are required.

The included GitHub Actions workflow builds the Railway Docker image, runs the offline tests inside it, and checks that the unprivileged app user can write the mounted database directory. No Discord or Claude credentials are needed for these checks.

## If it stays quiet

- Confirm the bot is online and `ALLOWED_CHANNEL_IDS` contains the current channel's exact ID.
- Enable Message Content Intent in the Discord portal and redeploy.
- Mention the bot or use Reply on one of its messages. For an older reply target outside its saved IDs/cache, enable **Read Message History** in that channel so it can verify the specific message. The reply mention/ping toggle can stay off.
- Wait out `COOLDOWN_SECONDS` (15 seconds by default); shared rate/concurrency limits can also prevent a request.
- Check Railway logs for authentication/model-access errors and inspect `!budget` as an owner. A depleted allowance deliberately stops paid replies.
- If startup reports volume permissions, keep the default Docker entrypoint and `/data` mount. Do not override it with a direct Python start command.

Before inviting others, tell them which channels save messages and learn style, and show `!privacy`, `!optout`, and `!forget`. Only the bounded context selected for an actual reply is sent to Anthropic; the passive archive stays on your Railway volume.
