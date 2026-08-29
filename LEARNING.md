# Learning, journal and insights

Three features that get better as data accumulates, plus the honest limits of
each. All of them stay **off** until they have enough data to be worth
trusting, and the **Learning** tab tells you what is missing and roughly how
long it will take.

---

## 1. Activity recognition

Bouts are found from heart rate and wrist movement, then labelled.

**Detection** (`segment.py`) is deliberately recall-oriented: it finds
candidate bouts and lets the classifier decide what they are. Expect some short
false positives; delete them or leave them as "other".

**Labelling** starts with fixed rules and low confidence. Every time you
confirm or correct one, that becomes a training example.

**Learning** (`ml.py`) is multinomial logistic regression — chosen because the
training set is dozens of examples, not millions, and a bigger model would just
memorise them. It also exposes per-feature weights, so it can say *why* it
called something a run rather than a walk.

It switches on when:

| Requirement | Value |
|---|---|
| Activity types with enough examples | 2 |
| Confirmed examples per type | 4 |
| Cross-validated accuracy | ≥ 60% |

Accuracy is measured by stratified k-fold cross-validation, not on the training
data, so it is not the model grading its own homework. Until all three are met
the rules stay in charge — a model that cannot beat "always guess the most
common class" is worse than the rules it would replace.

**Typical time to activate:** a few days. Most days produce several bouts, and
you need 8 confirmations total.

### Editing

- **Correct a type** — pick from the dropdown and Save. This is what trains it.
- **Fix the times** — a bout that started early or ran long can be adjusted.
- **Delete** — soft delete; re-detection will not bring it back.
- **Add manually** — for anything the strap could not see (an off-wrist class)
  or an event you want on the record.

Any edit marks the bout as yours, and re-detection will not overwrite it.
Manual entries are excluded from training, since they have no sensor features.

---

## 2. Journal

Tag your days — alcohol, caffeine, late meal, stress, travel, illness, and
anything you add yourself — plus free-text notes.

**Log ordinary days too.** A factor can only be tested if there are days both
with and without it. Journalling only the notable days makes the comparison
impossible, which is the single most common way this kind of analysis fails.

---

## 3. What helps and what hurts

The part most wellness apps get wrong. With 30 days and a dozen factors,
testing at p<0.05 produces roughly one false "discovery" every run *by
construction*. The guardrails here exist to stop that:

| Guardrail | Why |
|---|---|
| Factor on day D vs outcome on day **D+1** | Temporal ordering. "Worse recovery tomorrow" is defensible in a way a same-day correlation is not. |
| **Permutation tests**, not t-tests | 30–90 skewed days do not satisfy a t-test's normality assumption. Shuffling assumes nothing. |
| **Bootstrap confidence intervals** | An effect size without uncertainty invites over-reading. |
| **Benjamini–Hochberg FDR** across every test in the run | Without it, testing 15 factors manufactures false positives. |
| Minimum 5 days on **each** side, 12 journalled days total | Below that, refuse rather than compute a number from four days. |

A factor is reported as a signal only at **q ≤ 0.10**. Findings that do not
clear the bar are shown separately under "tested, no clear signal" — that is a
real result, not a failure.

**Everything here is association, never causation.** Days are not randomised.
If you drink on Fridays and also sleep badly on Fridays for unrelated reasons,
observational data cannot tell those apart.

**Typical time to activate:** about two weeks of consistent journalling.

---

## 4. Suggestions

Once there is a baseline, the Today tab offers plain-language suggestions —
"well recovered, a good day to push", "barely recovered, keep it light",
"about 9 hours of sleep debt this week". Each carries the numbers behind it and
a confidence level.

### The illness signal

Fires when **at least 2 of 4** markers deviate more than 1.5 standard
deviations from your own baseline:

- resting heart rate **up**
- HRV **down**
- skin temperature **up**
- respiration **up**

Skin temperature and respiration come off the strap as raw ADC counts that
cannot be converted to °C or breaths-per-minute without WHOOP's calibration.
That does not matter here: a count compared against *your own* baseline is
still meaningful, which is exactly what this needs.

**Read this before trusting it.** It detects *physiological strain*. Illness
causes that pattern — but so do alcohol, a hard session, heat, dehydration,
poor sleep and stress, and it cannot tell them apart. It is not a diagnosis, it
cannot say what is wrong, and its silence is not evidence that you are well.
Requires 14 days of baseline before it will fire at all.

---

## Tuning

| Setting | Where | Default |
|---|---|---|
| Examples per class before training | `ml.MIN_PER_CLASS` | 4 |
| Accuracy needed to use the model | `readiness.MODEL_MIN_ACCURACY` | 0.60 |
| Days each side of a comparison | `insights.MIN_GROUP` | 5 |
| Journalled days before analysis | `insights.MIN_PAIRS` | 12 |
| False discovery rate | `insights.FDR_Q` | 0.10 |
| Illness marker threshold | `advice.Z_FLAG` | 1.5 SD |
| Markers needed for illness signal | `advice.MIN_MARKERS` | 2 |

Loosening these makes features activate sooner and be wrong more often. The
defaults are deliberately conservative.

## Known limits

- **Sleep detection is threshold-based**, using your baseline resting HR. A
  night with a much elevated resting HR can fall outside the threshold and
  register as less sleep. The threshold uses the rolling *baseline* rather than
  the same night's value specifically to avoid a feedback loop, but the
  sensitivity remains.
- **Strain depends on `MAX_HR`.** The 190 default is `220 − age` at age 30. Set
  it in `.env` or every strain number is skewed.
- **Recovery, strain and sleep are approximations** from published methods, not
  WHOOP's models. They track your trends consistently; they will not match the
  official app.
