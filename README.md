# content-pipeline

Scans narratives, drafts posts, queues them for manual review. Unreviewed content is never posted.

## flow (MYT)
| time | what | where |
|---|---|---|
| 5:47am, 11:47am, 7:47pm | scout: Grok x_search per lane + DefiLlama + prices, writes `briefs/YYYY-MM-DD-HHMM.md` | GitHub Actions (`.github/workflows/scout.yml`) |
| 6:30am, 12:30pm, 8:30pm | drafter: reads profile + newest brief, saves up to 3 unscheduled drafts tagged `needs-review` | Claude scheduled task, follows `prompts/drafter.md` |
| whenever | review, edit, schedule | Typefully |

## files
- `profile/insomniac988.md`: voice, scope, format. edit this to change how drafts read
- `scout/lanes.yaml`: X handle groups (max 10 per lane), focus per lane, model
- `scout/scout.py`: the scout
- `prompts/drafter.md`: drafter instructions
- `briefs/`: scout output; `briefs/costs.csv` logs xAI cost per run

## run the scout manually
GitHub > Actions > scout > Run workflow

## secrets
- `XAI_API_KEY` (repo Settings > Secrets and variables > Actions)
