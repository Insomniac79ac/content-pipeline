# content-pipeline

Scans narratives, researches the best stories, drafts posts, lists reply targets, queues drafts for manual review. Unreviewed content is never posted.

## flow
A Claude scheduled task runs at 6:30am, 12:30pm and 8:30pm MYT and follows `prompts/drafter.md`:
1. **detect**: dispatches the `scout` workflow (Grok). open X searches every run; your X list (handle lanes) on the morning run only. writes `briefs/`
2. **learn**: checks which earlier drafts you posted, edited, ignored or deleted (`ledger/`)
3. **pick**: up to 3 post stories
4. **research**: dispatches `scout` in research mode for the 3 stories (Grok X deep dive + DefiLlama), plus its own web search for primary sources. writes `research/`
5. **write + save**: drafts to Typefully, unscheduled, tagged `needs-review`
6. **reply targets**: 5 to 8 fresh posts to reply to, in the run summary and `replies/latest.md`

You review, edit and schedule in Typefully.

## files
- `profile/insomniac988.md`: goals, topics, voice rules, bans, formats
- `profile/voice-examples.md`: real posts the drafts are matched against
- `profile/products.md`: Thetanuts, Odette, ZendFi facts and angles
- `voice/`: raw pulls of the account's own posts (source for the examples)
- `prompts/drafter.md`: the run steps
- `scout/lanes.yaml`: lanes, handle groups (max 10 each), model, `research_x` switch
- `scout/scout.py`: Grok detect + research
- `briefs/costs.csv`: xAI cost per run
- `ledger/drafts.jsonl`, `ledger/learnings.md`, `ledger/followers.csv`: feedback loop
- `replies/latest.md`: latest reply targets

## manual run
GitHub > Actions > scout > Run workflow (mode: detect, research, engagement, voice or history)

## secrets
- `XAI_API_KEY` (repo Settings > Secrets and variables > Actions)
