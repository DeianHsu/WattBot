# WattBot 2026 — team writeup

The writeup lives in your submission repo, not on Kaggle: copy this file into your repo as
`WRITEUP.md`, fill it in, and make sure it is included in the tag you submit — the same tag
that produced your leaderboard score, so report, code, and number are one pinned,
verifiable artifact. Then post one comment in the pinned **Writeup Index** Discussion
thread: your submission card verbatim plus the repo link. Edit that comment in place as
your pipeline improves.

Required from **top-3 teams** at close — verification re-runs your code at the tag and
checks it reproduces your predictions. Welcome from everyone else, and MLM26 event teams
submit one as part of the Marathon's milestones regardless of rank.

Aim for **≤2,500 words**. The structure below is a suggestion, not a form — the important
thing is the learning journey: what you tried, what worked, what didn't, where you ended
up.

## Submission card

Open the report with this block, filled in:

```nix
code_url: https://github.com/team/pipeline/tree/v1.0-submission
writeup_url: https://github.com/team/pipeline/blob/v1.0-submission/WRITEUP.md
generation_model: Qwen3-32B             # what writes the answers
embedding_model: bge-m3                 # what builds the index; "none" if lexical-only
reranker_model: none                    # "none" is fine
other_models: none                      # anything else in the answer path — vision/OCR, a judge, a router
leaderboard_score: 0.734                # your selected submission's WattBot Score
external_data: none                     # fine-tuning data, extra corpora — a disclosure, not a requirement
hardware: 1x RTX 4090 / Bedrock         # informational, not scored
cost_estimate: ~$12 API spend           # informational, optional
wall_clock: 2.5 h for the full test set # informational, not scored
```

`code_url` must be a public repo pinned to the exact tag or commit that produced your
leaderboard submission (`git tag v1.0-submission && git push origin v1.0-submission`).
Open the link in a private browser window before posting — if it 404s, the repo is private
or the tag isn't pushed. `writeup_url` is a direct link to `WRITEUP.md` at that same tag,
so a reader lands on the report rather than a repo root. The four model fields together must cover
everything in the answer path, closed APIs included — a model not named in the card and
found in the code reads as an omission at verification time. `hardware`, `cost_estimate` and `wall_clock` are never
scored — together they tell the next cohort what is realistic.

## Approach

*Two or three paragraphs. What retrieves, what generates, what glues them together.*

- **Retrieval:** [chunking, index, embeddings or lexical, reranking, top-k]
- **Generation:** [model(s), prompting, any fine-tuning]
- **Anything unusual you tried** — including things that didn't work. Negative results are
  genuinely useful to the next cohort.

## Knowing when not to answer

*Some questions cannot be answered from the corpus, and answering one anyway scores zero
on `answer_value`. How did your system decide to abstain?*

[e.g. retrieval-score threshold, self-consistency across samples, an explicit "is this
supported?" check, nothing at all]

## Handling sources that disagree

*Several corpus documents restate the same figure at different precision or scope — site
vs source water, a rounded value vs the original — and some genuinely conflict. How did
your pipeline choose, and did it ever reconcile rather than pick?*

[your answer]

## Citations

*Citation scoring rewards precision as well as recall — citing extra documents costs you.
How did you decide which documents to cite versus merely retrieve?*

[your answer]

## What you'd do with another week

[your answer]

---

*Optional but appreciated: if you found a question you believe is wrong, ambiguous, or
answerable from a document the ground truth doesn't cite, say so here. We fix these and
rescore.*
