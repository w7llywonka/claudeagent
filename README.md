# Discord Haiku chat bot

A small, text-only Discord bot for Railway. It uses **Claude Haiku 5.5 at low effort with thinking disabled**, keeps replies short, and adapts its writing style using local statistics. By default, it saves new human text messages in allowed channels and learns their writing style locally, even when nobody mentions it. It calls Claude for **`/ask question:your message`**, a mention, a reply to the bot, or a message in an explicitly enabled automatic-reply channel.

Defaults: **one chat request per person every 15 seconds**, **120 output tokens**, **45 words**, **300 characters**, and shared allowances of **$0.50/day, $5/month, $5 total**. The API output limit has an absolute ceiling of **20,000 tokens**, even when changing configuration. Leave the default at 120 for ordinary conversation.

The bot cannot run commands, browse links, use tools, or change its settings through a conversation. No language-model prompt can guarantee perfect jailbreak resistance; capabilities, budgets, rate limits, and final reply size are enforced by application code. It can recall relevant parts of your earlier conversations from local storage, alongside facts you explicitly save. This is bounded conversational memory, not perfect recall or model retraining.

## Get it online

### 1. Create the Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications), create an application, and open **Bot**.
2. Create/reset the bot token and keep it for Railway's `DISCORD_TOKEN` variable. Enable **Message Content Intent** under Privileged Gateway Intents. The bot needs this to read allowed-channel conversation and commands. Larger verified bots may require approval. [Discord intent documentation](https://docs.discord.com/developers/events/gateway#message-content-intent)
3. Install the application into your server with the **bot** and **applications.commands** scopes. Grant **View Channel**, **Send Messages**, and **Read Message History** in the intended ordinary text channel. Read Message History lets the bot verify an older reply target if Discord omits it from the event. Administrator permissions are unnecessary. If using a thread, also grant **Send Messages in Threads**; add that thread's own ID to the allowlist if you want ambient learning or mention-triggered chat there.
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

The container briefly starts its entrypoint as root to prepare the exact database paths, then drops to the unprivileged `app` user before starting Python. The volume preserves the message archive, memory, opt-outs, cooldowns, and spending records over restarts in `/data/chat.db`. Start with a **256 MB volume for the default archive limits** and monitor free space. `MESSAGE_MAX_MB` caps stored message text, **not the database or volume size**. The full-text search index uses additional disk space, even though it does not duplicate the message bodies; SQLite's other indexes, write-ahead log and bot records also need headroom. Increase the volume if needed or if you raise the archive limits. Railway volumes mount at runtime, cannot be shared across replicas, and may introduce brief deployment downtime. [Volume setup](https://docs.railway.com/volumes), [volume limitations](https://docs.railway.com/volumes/reference)

Setup uses Railway's dashboard deliberately: its older `railway.json`/`railway.toml` Config as Code is deprecated, with legacy support documented through December 1, 2026. [Railway configuration documentation](https://docs.railway.com/config-as-code/reference)

### Add `/ask` to your Discord account

At startup, the bot tries to enable both **User Install** and **Guild Install**, adds missing command scopes while preserving the application's existing installation permissions, and registers `/ask` globally. Look for **`Registered global /ask`** in Railway's logs. Commands use the existing Discord Gateway connection; no public HTTP endpoint is needed. [Discord command contexts](https://docs.discord.com/developers/interactions/application-commands#contexts)

Open the app's Discord profile or installation link and choose **Add to My Apps** to install it to your account. Then use **`/ask question:your message`** in a supported server, DM or group DM. An existing installation may need to be refreshed or added again before `/ask` appears in the command picker.

**Add to My Apps alone cannot receive ordinary Discord replies in a server where the bot is absent, or in other people's DMs/group DMs.** Use `/ask` there. For native Reply conversations in a server, add the bot to that server and let it view and send messages in the channel. Direct conversations with the bot itself also support native replies. This is a Discord installation constraint, not a Claude limitation. [Discord app access](https://support.discord.com/hc/en-us/articles/21334461140375-Using-Apps-on-Discord)

If automatic setup fails, open the application's **Installation** page in the Discord Developer Portal. Under **Installation Contexts**, enable **User Install** and **Guild Install**, then save. Under **Default Install Settings**, give **User Install** the `applications.commands` scope and **Guild Install** both `applications.commands` and `bot`; keep the server permissions listed above. Choose **Discord Provided Link** under **Install Link** if the app does not already have a usable installation link. That link offers **Add to My Apps** or **Add to Server**. Redeploy to retry command registration. [Discord user-install setup](https://docs.discord.com/developers/tutorials/developing-a-user-installable-app)

Public answers depend on Discord's channel permissions. **Use External Apps** controls public replies from a user-installed app that is not also installed to that server; **Send Messages** can also restrict public responses. [Discord permissions](https://docs.discord.com/developers/topics/permissions), [application response restrictions](https://docs.discord.com/developers/resources/application). When Discord permits only a private response, `/ask` returns a private explanation and does not call Claude or store the question. **`/ask question:!forget`** and **`/ask question:!optout`** still work in that situation and confirm privately.

### 3. Choose when it talks

- **Slash command:** use **`/ask question:your message`** for a public reply. The app registers `/ask` globally at startup for server and user installations. A user-installed app can receive explicit asks in Discord locations where that installation is available, including outside this bot's ordinary channel allowlist. `/ask` uses the same model, cooldown, shared rate limit, concurrency limit and budget as ordinary bot conversations. It needs no additional environment variables.
- **Mention:** mention the bot in an allowed channel.
- **Native Reply:** use Discord's **Reply** action on one of the bot's messages, including a public `/ask` answer. Verified replies work outside the ambient channel allowlist and in direct DMs with the bot, whenever Discord delivers the message to the bot. The reply's mention/ping toggle can be off. Reply detection checks the Discord event, local cache, and up to 4,096 persisted bot-message IDs, which survive restarts. If needed, it fetches only the exact message being replied to and checks its author; it never scans channel history. The fallback requires **Read Message History** and is limited to 20 lookups per minute, two concurrent lookups, and a five-second timeout. No Claude tokens are used for this check.
- **Dedicated chat channel:** set `AUTO_REPLY_CHANNEL_IDS` to a subset of `ALLOWED_CHANNEL_IDS`. Every human message there is eligible for a reply, subject to the same limits.
- **Ordinary direct messages:** set `ENABLE_DMS=true` to allow regular DM chat without using Reply. It is disabled by default; explicit `/ask` and verified replies to the bot work independently.

An invalid `ALLOWED_CHANNEL_IDS` prevents startup. An empty list permits no ambient server-channel messages and prevents startup unless DMs are explicitly enabled. Ordinary messages outside the allowlist are ignored unless they are verified replies to this bot; explicit `/ask` commands are also handled separately. Neither exception enables ambient collection in that channel. Other bots and webhooks do not trigger conversations. A person's chat cooldown applies across channels and survives restarts; it is not a separate allowance in every channel.

In an allowed server channel, `/ask` and native replies share the channel's existing recent conversation and style. In a DM or outside an allowed server channel, explicit `/ask` conversations and native replies use the same memory, isolated by **person and channel**, so switching between them keeps your context. One person's external conversation history is never supplied as another person's context. Installing the app to a user account does not enable passive collection of ordinary messages outside the configured allowlist. `/ask` questions are public, so do not put private information in them.

Change `COOLDOWN_SECONDS` in Railway variables and redeploy to adjust it whenever you want. The default is `15`; `0` means **no per-user cooldown**. Whole-number values from `0` to `86400` are accepted. Shared rate limits, spending limits and one in-flight reply per user/channel still apply, including at zero cooldown. A native reply blocked by the cooldown gets a brief local notice, limited to one notice per person every 15 seconds; this costs no Claude tokens. These reply limits do not stop passive archiving or local style updates: eligible messages are still observed during a cooldown or when the API allowance is exhausted.

## Commands

`/ask question:your message` chats with Claude. The following text commands run locally and do not call Claude. They also work inside the question field, such as **`/ask question:!forget`** or **`/ask question:!optout`**, including from a user installation outside an allowed bot channel. These two privacy commands confirm privately and apply to all of that person's stored data across the bot.

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

With `PASSIVE_LEARNING=true`, the bot archives **every new eligible human text message it receives in allowed channels**, including unmentioned messages and messages sent during reply cooldowns, API outages or after the API budget runs out. Bots, webhooks, ordinary messages in disallowed channels and opted-out users are excluded. Explicit `/ask` questions and verified replies to the bot are eligible even outside the allowlist, using the isolated memory described above. `!forget` and `!optout` are never retained. Other commands and obvious instruction-override attempts are archived for eligible users but excluded from style learning and prompt recall. Stored text is sanitized and capped at 4,000 characters per message; Discord message or interaction IDs prevent duplicate recording. Attachments are not downloaded, linked pages are not read, and old Discord history is not backfilled.

The archive stays local in SQLite on the Railway volume. Its default limits are **30 days, 50,000 messages, and 64 MiB of stored message text** across the bot. The oldest messages are removed when a row-count or text-size limit is reached; expired rows are cleaned on startup and roughly hourly. The text-size limit counts UTF-8 bytes, not the whole database file; the search index and SQLite write-ahead log are additional storage. Set `MESSAGE_RETENTION_DAYS` (1–365), `MESSAGE_MAX_ROWS` (100–1,000,000), and `MESSAGE_MAX_MB` (1–128) to change these limits. `!forget` and `!optout` still clear the user's archived content.

Each ordinary eligible message incrementally updates small numeric style statistics for its author. Commands and obvious instruction-override attempts do not affect those statistics. A channel-wide style summary averages the stored user profiles. This uses no paid AI calls for ambient conversation and does not reread the raw archive to learn on each reply. It adapts writing habits; it does not retrain Claude or update model weights. Set `PASSIVE_LEARNING=false` to restrict archiving and style updates to triggered conversations instead of ambient channel chat.

## How it stays inexpensive

Each Claude request includes a compact style summary and a small recent conversation window: by default at most six recent context entries, with a maximum of eight, expiring after 30 minutes. To recall older conversations, a local **SQLite full-text search** selects up to **three excerpts of at most 240 characters each** from that person's archived messages in the same channel. It searches for relevant words, falling back to a small slice of that person's recent archive when there is no match or the question is broad. The current message is excluded, and identical excerpts already present in the request are removed. The bot never loads or sends the full archive for a reply.

This search uses the existing archive limits and costs no extra AI calls. There are no embeddings, vector database, or background AI summarizer. Recall depends on relevant words and retained history; it will not remember every detail or recover expired/deleted messages. More stored messages improve the pool it can search without making the per-request context grow without a bound.

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
| `MESSAGE_MAX_MB` | `64` | Maximum archived text in MiB of UTF-8 bytes, 1–128; excludes the search index, WAL and other SQLite overhead |
| `CACHE_SYSTEM_PROMPT` | `false` | Optional static policy cache marker |
| `ENABLE_DMS` | `false` | Allow ordinary direct messages without Reply; `/ask` and verified replies work independently |
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

- Confirm the bot is online and has been added to the server, with permission to view and send messages in the channel. Mentions and ambient learning also require the current channel's exact ID in `ALLOWED_CHANNEL_IDS`; verified native replies do not.
- Enable Message Content Intent in the Discord portal and redeploy.
- Mention the bot or use Reply on one of its messages. For an older reply target outside its saved IDs/cache, enable **Read Message History** in that channel so it can verify the specific message. The reply mention/ping toggle can stay off.
- If you used **Add to My Apps** without adding the bot to that server, ordinary replies are not delivered to the bot. Continue with `/ask`, or add the bot to the server. In someone else's DM or a group DM, use `/ask`.
- For `/ask`, confirm the app is installed to your account or server and check the startup log for command registration. If it is absent from the picker, refresh or re-add the installation. A slash command gets a short local explanation when a shared limit or cooldown blocks it. A private permission notice means Discord did not permit a public answer; no Claude call was made.
- Wait out `COOLDOWN_SECONDS` (15 seconds by default); native replies receive a rate-limited local notice rather than spending Claude tokens. Shared rate/concurrency limits can also prevent a request.
- Check Railway logs for authentication/model-access errors and inspect `!budget` as an owner. A depleted allowance deliberately stops paid replies.
- If startup reports volume permissions, keep the default Docker entrypoint and `/data` mount. Do not override it with a direct Python start command.

Before inviting others, tell them which channels save messages and learn style, and show `!privacy`, `!optout`, and `!forget`. Only the bounded recent context, relevant excerpts and facts selected for an actual reply are sent to Anthropic; the full archive stays on your Railway volume.
