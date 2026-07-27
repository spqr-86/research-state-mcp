# FRAMES recall — measured results

First live measurement of the fragment layer, 2026-07-27.

## What is measured

`eval/frames_recall.py` takes FRAMES examples (google/frames-benchmark, Apache-2.0,
824 examples, split `test`), downloads the Wikipedia articles listed in each
example's `wikipedia_link_*` fields, runs `fragments.extract` over them with the
question as the query, and checks whether the gold answer survived into the
returned fragments.

The server does not search and does not reason. This measures the one thing it
does do: throw away most of a page without throwing away the answer.

Reproduce (the page cache makes every rerun free):

```bash
uv run python eval/frames_recall.py --limit 100 --k 5
uv run python eval/frames_recall.py --limit 100 --k 5 --raw-text
```

## The baseline problem — read this before the numbers

**The gold answer is literally present on the linked pages in only 45 of 100
examples.** FRAMES is a multi-hop benchmark: answers are usually computed across
sources ("how many years older", "who was mayor that year"), not quoted from one.
Those 55 examples say nothing about the fragment layer — no wording exists for it
to find. Every headline number below is therefore the recall **among the 45**,
and `answer_on_page` is reported alongside it so the denominator stays visible.

This is the main limitation of using FRAMES here. A single-hop subset, or a
different corpus, would give a larger denominator.

## Budget is the binding constraint, not ranking

100 examples, paragraph mode, varying `k` (budget shared across the whole example):

| k | recall (of those present) | text returned |
|---|---|---|
| 1 | 11.1% | 2.9% |
| 3 | 33.3% | 11.0% |
| 5 | 51.1% | 17.2% |
| 10 | 68.9% | 33.0% |
| 20 | 86.7% | 56.9% |

Giving each page its own budget of `k=5` instead of sharing one budget across the
example's 3.4 pages raises recall from 51.1% to **77.8%** at 17.2% of the text.

So: call `fragments_for` once per source with its own `k`, rather than trying to
fit a whole question into five fragments. The skill already does this — now there
is a number behind it.

## Unstructured pages cost about a third of the accuracy

`--raw-text` feeds the article in without re-splitting it into paragraphs, which
is what pages out of the free-search-mcp cache actually look like (markdown from
HTML, tables, no guaranteed paragraph breaks). There the `MAX_PARAGRAPH_CHARS`
emergency cutter runs, slicing 2000-character pieces.

Raw at `k=5` scores *higher* than paragraphs at `k=5` — 62.2% vs 51.1% — but that
comparison is worthless: a 2000-character slice is bigger than a paragraph, so raw
also returned 25.6% of the page instead of 17.2%. More text, more chances to catch
the answer.

Equalising the text returned tells the real story:

| mode | text returned | recall (of those present) |
|---|---|---|
| raw (k=3) | 15.3% | 40.0% |
| paragraphs (k=5) | 17.2% | **51.1%** |

**Per unit of returned text, paragraph structure beats fixed-size slicing by about
half again.** The cutter added on 2026-07-26 is a brake against handing back a
whole page, not a substitute for structure. That is why `fragments_for` returns a
`note` when a page has no paragraph breaks: it warns of a real accuracy drop.

## Honest caveats

- The grader is a normalised word-boundary substring match. Numeric formatting
  differs ("5,000" vs "5000") and paraphrase is never credited, so `answer_on_page`
  understates the true baseline.
- Short answers (< 4 normalised characters) are reported separately: 15 of 100,
  11 on page, recall 54.5%. They are the least reliable part of the measurement.
- bm25 scores come from independent per-page indexes and are formally
  incomparable, so the per-example pool is conservative — it understates rather
  than flatters.
- One example was skipped: a `wikipedia_link_*` field held a search URL rather
  than an article.
