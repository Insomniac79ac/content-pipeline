# content-pipeline

Scans narratives, researches the best stories, drafts posts and replies, queues them for manual review. Unreviewed content is never posted.

## flow
A Claude scheduled task runs at 6:30am, 12:30pm and 8:30pm MYT and follows `prompts/drafter.md`:
1. **detect**: dispatches the `scout` workflow (Grok). open X searches every run; your X list (handle lanes) on the morning run only. writes `briefs/`
2. **learn**: checks which earlier drafts you posted, edited, ignored or deleted (`ledger/`)
3. **pick**: 3 post stories + 2 reply targets
4. **research**: dispatches `scout` in research mode for the 3 stories (Grok X deep dive + DefiLlama), plus its own web search for primary sources. writes `research/`
5. **write + save**: drafts to Typefully, unscheduled, tagged `needs-review`

You review, edit and schedule in Typefully.

## files
- `profile/insomniac988.md`: voice, structure, series formats, reply rules. edit this to change how drafts read
- `prompts/drafter.md`: the run steps
- `scout/lanes.yaml`: lanes, handle groups (max 10 each), model, `research_x` switch
- `scout/scout.py`: Grok detect + research
- `briefs/costs.csv`: xAI cost per run
- `ledger/drafts.jsonl`, `ledger/learnings.md`: feedback loop

## manual run
GitHub > Actions > scout > Run workflow (mode: detect or research)

## secrets
- `XAI_API_KEY` (repo Settings > Secrets and variables > Actions)
