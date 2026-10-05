# Contributing questions

Post contributions in the **Contributing questions** thread in Discussion.

Everything you contribute joins the **train** split, never the test set, so you can never
be scored on a question you wrote. Contributors are credited in the final results post.

## The workflow

**1. Ask a question you actually want answered.** Not a question engineered to be hard —
one you would type into a chatbot if you trusted it. *How much water does a data center in
my state use? Does switching to a smaller model actually save energy, or just move it? What
does a company's reported carbon number leave out?*

**2. Find the paper that answers it.** Literature search tools built for this — OpenScholar,
Semantic Scholar, arXiv search — are the fastest route from a question to the paper holding
the number. If the paper is not already in `metadata.csv`, say so: we add papers, and a
question that brings a new source with it is worth more than one that does not.

**3. Verify the number is really in the paper.** Open it. Find the sentence, the table cell,
or the figure. If you cannot point at it, the question is not ready.

**4. Write it as a `train_QA.csv` row.** Same columns, same conventions. Copy a few rows out
of `train_QA.csv` and match what they do.

## What makes a contribution valuable

Ordinary lookups are welcome, but the bank is thinnest on these:

- **Two papers, one answer.** GPU specs in one, an emissions rate in another, the answer in
  neither. Don't reveal which papers in the question text.
- **A value only in a figure.** Read off a chart with no sentence stating it. Give a range,
  not a false-precision point: `[0.33,0.47]` is the honest gold for a bar height.
- **A claim, attributed.** A number from a company's own reporting, phrased so a correct
  answer treats it as that company's claim.
- **An apparent conflict that isn't one.** Two sources with different numbers for what looks
  like the same quantity — different system boundary, different service target, different
  year. The answer comes from reading what each measured.
- **Genuinely unanswerable.** A reasonable question this corpus cannot support. Set
  `answer_value`, `ref_id`, `ref_url` and `supporting_materials` all to `is_blank`. These are
  scored, and they are hard to write well.

## What gets rejected

- The answer is not actually in the cited paper.
- The answer is a paraphrase where several wordings are equally correct — a scored answer
  has to be one number, one term, or True/False.
- The question quotes the passage that contains its own answer.
- The question names the paper, which turns retrieval into a lookup.
- Bulk submissions produced by prompting a chatbot and not checked against the sources. One
  verified question beats twenty unverified ones, and a batch found to contain fabricated
  evidence is discarded whole.

## Format

```
id                     leave blank, we assign it
question               the question, standalone
answer                 natural-language response
answer_value           the number, term, or 1/0 for True/False — or is_blank
answer_unit            unit, or is_blank
ref_id                 document id(s) from metadata.csv, or is_blank
ref_url                url(s), or is_blank
supporting_materials   the quote, table, or figure you verified against
explanation            how the evidence gives the answer, including any arithmetic
```

Ranges: `(low,high)` when the source states a range and both endpoints are the answer;
`[low,high]` when the answer is derived or read off a chart and any value in the band is
correct.

One question per block. A short note on why you thought it was worth asking is welcome and
sometimes more useful than the question.
