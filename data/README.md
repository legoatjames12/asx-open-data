# Open data collected by collect.py

Everything in this folder comes from public government sources and is refreshed every hour by `collect.py`.
`status.json` says how each source went on the latest run. `state.json` is the collector's own memory.

| Folder | What it holds | Source and credit |
|---|---|---|
| `feeds/` | Headline, link and short extract of each new announcement, one file per Sydney day | The publishers' own RSS feeds. Each line names its source and links to the original. Copyright stays with each publisher. |
| `short-positions/` | Daily aggregate short position reports, unaltered | © Australian Securities & Investments Commission: Reproduced with permission. Licensed CC BY 4.0. |
| `us-treasury/` | Daily par yield curve rates, one file per year, unaltered | U.S. Department of the Treasury. Public domain. |
| `abs/` | Monthly Consumer Price Index and Labour Force series, unaltered | Source: Australian Bureau of Statistics. Licensed CC BY 4.0. |
| `contracts/` | Commonwealth contract notices, one file per Sydney day of publication | Based on AusTender data, © Commonwealth of Australia, licensed CC BY 3.0 AU. Fields have been selected and renamed; `sample-release.json` shows one notice as published. |

No source here endorses this collection or any use made of it.
