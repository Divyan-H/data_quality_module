# City names: how to flag the same city written differently, without slowing the pipeline

Design note, 2026-10-29. It answers: can we store the cities somewhere and look them up by hash, how many cities would that be, what else could we do, and how do we keep it fast. The chosen design is built (rule D-30, `src/hygiene/cities.py`).

## 1. What has to be found

| Case | Example | Why a plain lookup is not enough |
|---|---|---|
| Same city, different name | Bombay and Mumbai, Bangalore and Bengaluru | Needs knowledge: no spelling rule links them |
| Short form | BLR, Mum, Hyd | Needs knowledge, and short forms are ambiguous (`Pun` is not always Pune) |
| Spelling mistake | Dehli, Banglore, Hyderbad | Needs fuzzy matching |
| Case and spaces | `pune`, `New-Delhi` | Trivial with normalising (D-10 already unifies case) |
| Same name, different city | Springfield (many US states), Aurangabad (two Indian states) | **Must not be merged**: needs another column (state, country) |
| Region, not a city | Delhi NCR | Must stay its own value |

## 2. How many cities would a store hold, and is size the problem?

Rough sizes, from the public GeoNames files (figures are approximate; check the current files before relying on them):

| Source | Places | Notes |
|---|---|---|
| Cities with 15,000+ people | about 25,000 | One name each, plus alternate names |
| Cities with 5,000+ people | about 50,000 | |
| Cities with 1,000+ people | about 140,000 to 150,000 | |
| All populated places | millions | Villages, hamlets: not "cities" |

**A hash holds all of that easily.** A Python dictionary of 150,000 names is tens of megabytes and a lookup is constant time. Storage is not the real problem. The real problems are:
1. **Coverage of other names and misspellings.** Gazetteers list official and some alternate names, not every typo or short form clients type.
2. **Ambiguity.** The world has many cities with the same name; a global list cannot say which one a value means.
3. **Fuzzy lookup against a large list.** Comparing every value with every name is the slow part, not the hash.
4. **Upkeep and licensing.** A big file has to be updated, and its licence respected.

## 3. All the ways to do it

| Approach | Good | Bad | Verdict |
|---|---|---|---|
| **A. Hash of names and aliases** (exact, after normalising) | Instant; deterministic; no false matches | Only finds what was listed | **Use** (tier 2) |
| **B. The project's own city list** | Small, exact, owned by the client; makes "not in our list" decidable; the project's spelling becomes the standard | Needs the list from the client | **Use**: best signal available |
| **C. A global gazetteer** (GeoNames and similar) | Wide coverage; offline | Large; ambiguity; does not cover short forms or typos | **Optional**, loaded only for the project's countries |
| **D. Edit distance against a list** | Finds typos | Comparing with every name is slow if done naively | **Use**, with an index (below) |
| **E. Delete-neighbourhood index** (the SymSpell idea) | Finds names within 1 or 2 edits in time that depends on the word length, not on the dictionary size | Memory grows with the list and edit distance | **Use** (tier 3) |
| **F. Phonetic keys** (Soundex, Metaphone) | Catches sound-alikes | Built for English; weak for Indian names; many false matches | Skip |
| **G. n-gram or trigram index** | Good for long strings | Heavier than E for short city names | Skip |
| **H. Learn from the data itself** (compare unknown values with each other) | Finds a misspelling of a city that is in no list | Cannot say which spelling is right, only which is more frequent | **Use** (tier 4) |
| **I. Geocoding web service** | Understands any place | Sends client data out; slow; costs money; rate limits | **No**: breaks "data stays local" |
| **J. A language model** | Understands context | Slow, can invent matches, needs hardware | Not in the pipeline; at most a last resort for a human to ask |
| **K. Ask a human for everything unknown** | Always right | Does not scale | Needed for what is left after tiers 1 to 4 |

## 4. The design

**Rule of speed: work on distinct values, never on rows.** A column of 5 million rows holds a few hundred distinct city spellings at most. Every tier runs once per distinct value; the rows are touched once, to apply an answer.

```
column of cells --> count distinct values (one pass)
                       |
         for each distinct value, stop at the first answer:
           1. normalise           (case, accents, punctuation, spaces)
           2. exact hash lookup   O(1)   project list > city_names.yaml > optional gazetteer
           3. close lookup        delete-neighbourhood index, then a real edit distance on the few candidates
           4. unknown values compared with each other (blocked by their first letters)
           5. what is left: with a project list, "not in the list"; without one, nothing is said
                       |
         one review item per distinct spelling (not per cell), proposed standard name
                       |
         one answer in the review workbook fixes every cell with that spelling
```

**Why it does not choke the pipeline:**
- **Cost scales with distinct values, not rows.** Matching 2,000 values took well under 5 milliseconds each in the tests, and building the index takes a fraction of a second.
- **The index is built once per process** and cached by file time stamp. Phase B runs in one worker process per job, so it is built once per run, not once per table or column.
- **Only city columns are looked at** (column name contains `city`, `cities` or `town`; the words are a setting).
- **Flag only.** The cells are held back from later rules and nothing is changed until someone answers, so a wrong guess costs one click, not wrong data.
- **One item per spelling, with a cap** (300 per column by default, the total is reported), so a messy column cannot flood the review workbook.

**What is stored, in order of authority:**
1. **The project's own city list** (`cities:` in the run inputs). Its spellings become the standard, and any other value is flagged. For a project with a list, a world gazetteer is not needed at all.
2. **`config/city_names.yaml`**: about 80 Indian cities with their other names, old names, short forms and common misspellings. It is a plain text file anyone can edit; adding a city is one line.
3. **An optional gazetteer** (`city_gazetteer`: a csv with name, canonical, country), filtered to the project's countries (`city_countries`), for wider coverage.

**What it will not do**, on purpose:
- It does not merge two different cities that share a name; a name listed for two cities is ignored, and a value equally close to two cities is left alone.
- It does not merge `Delhi NCR` into `Delhi`.
- It does not decide between two unknown spellings except by frequency, and it says so.

## 4b. With a project list: what is flagged and how urgently (added 2026-10-29)

| Value | Flag | Severity |
|---|---|---|
| In the project's list | none | |
| Another name or short form of a listed city | propose the listed spelling | warn |
| A spelling mistake of a listed city | propose the listed spelling | warn |
| A real Indian city that is not in the list (`config/india_cities.txt`, plus the alias file) | "a known city that is not in the project's list" | **info** |
| A spelling mistake of such a city | propose the known spelling | warn |
| In none of the lists | "not in the project's list" | warn |

The review lists `warn` items first. The fallback list is a plain file (one city per line) and never overrides the alias file.
It is a starting list of about 500 names, not every town in India; a town missing from it shows as `warn` until it is added.

## 5. Limits and next steps

| Limit | What to do about it |
|---|---|
| The alias file covers India only | Add more cities, or load a gazetteer for the project's countries |
| A very large gazetteer makes the delete index big (millions of entries) | Keep it to the project's countries, or keep the index in an on-disk database; the lookup code does not change |
| Same name in different states or countries | Use a state or country column next to the city when one exists (not built yet) |
| Short forms like `Pun` are guesses | They are only proposals; the client answers |
| Answers are not remembered for the next upload | Save accepted spellings per client (not built yet) |

## 6. How it connects to the other new checks

The project list for cities lives in the same run inputs file as the expected columns and the currency (`config/run_inputs.yaml`, or a run's own copy, read and written through `GET` and `PUT /api/v1/runs/{id}/inputs`). One place holds everything the project tells us after uploading its files.
