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

---

# Advanced analytics

Seven further models, in the **Body** tab. Each states what it can and cannot
support.

## Non-linear HRV — DFA α1 and sample entropy

RMSSD measures how *much* beat-to-beat variability there is. DFA α1 measures its
*structure* — whether the variation is correlated or random — which tracks
autonomic balance rather than autonomic volume.

| α1 | Reading |
|---|---|
| > 0.75 | correlated, low-intensity / aerobic |
| 0.5 – 0.75 | transitional |
| < 0.5 | uncorrelated, at or past the aerobic threshold |

The implementation is validated against signals with known scaling exponents:
white noise → 0.5, 1/f pink noise → 1.0, Brownian → 1.5, all within 0.12.

**α1 is exquisitely artifact-sensitive.** One missed beat in a few hundred can
move it by 0.2 — the width of an entire band. So beats are cleaned against a
rolling local median, the artifact rate is reported with every value, and above
5% artifacts nothing is reported at all. The reference bands come from
chest-strap studies; a wrist optical sensor is a noisier source.

## Circadian phase — HR trough timing

The clock hour of the lowest smoothed heart rate, and where it falls in the
night. A trough in the first half is the settled pattern; pushed into the second
half it commonly follows late eating, alcohol, a warm room or a shifted body
clock. **It measures the timing, not the cause** — it cannot tell those apart.

## Recovery velocity

How far heart rate falls 1, 5 and 15 minutes after the day's hardest bout, plus
the fraction of the way back to rest at 5 minutes (scale-free, so comparable
across sessions of different intensity). Steeper is faster parasympathetic
reactivation.

## Training load — EWMA ACWR

Acute (7-day) against chronic (28-day) load, exponentially weighted at the
standard `2/(N+1)`, so recent days count for more than a flat average gives
them.

**No injury-risk number is shown, deliberately.** The popular "above 1.5 means
injury risk" claim comes from work that has been widely challenged for
mathematical coupling and spurious correlation. The ratio describes your load
trend; that is what it can honestly support.

## Sleep debt payback

Debt compounds rather than summing: extra sleep pays it down at 55% of face
value and old debt fades ~6% a night. The plan is solved by simulating the same
model forward, so the figure and the plan cannot drift apart, and the answer is
rounded **up** so the number reported genuinely clears the debt it claims to.
The constants are a convention, not physiology.

## Today's strain target

Fits next-day recovery on today's recovery and today's strain (ridge, small-n),
then solves for the strain that still leaves tomorrow at or above your floor.
Needs 21 paired days. If the fit is weak (R² < 0.15) or says strain costs you
nothing, it falls back to a standard recovery-to-strain curve and says so.

## Effort efficiency — the honest caveat

Falling heart rate at the same external workload means adaptation; rising means
under-recovery or illness. That needs a measure of external workload, and **this
strap has none** — no GPS, no power meter, no cadence. The only movement signal
is wrist acceleration.

So "output" here is wrist movement intensity: a fair proxy for running and
walking, weak for rowing, and not meaningful for cycling, lifting or swimming.
Only locomotion types are reported; the rest are skipped with the reason rather
than given a fabricated number. Pair the strap with phone GPS or a power meter
and this becomes a real efficiency measure.

## Caffeine and alcohol overlay

Logged with timestamps (in **Journal**), because the point is how much is still
circulating at lights out.

**These clear differently, and modelling both as a half-life is wrong for one.**
Caffeine is first-order — about a 5-hour half-life. Alcohol at ordinary doses is
**zero-order**: cleared at a near-constant ~1 standard drink an hour regardless
of how much is aboard. An exponential model would badly understate how long a
heavy night lingers.

Correlated against overnight HRV, resting heart rate and sleep duration.
**Not** against slow-wave sleep percentage — sleep staging needs signals this
strap does not expose, so it is not estimated.

---

# Detail views

Three cards on the main feed open a full screen of their own, each on its own
hash route (`#/health`, `#/stress`, `#/age`) so they are linkable and the
browser's back button behaves the way people expect. Tapping anywhere on the
Health monitor or Stress card on **Today**, or the Fitness age card on
**Body**, opens the matching view.

## Health monitor — `#/health`

Five channels, each with a *personal* normal range rather than a population
one: resting heart rate, HRV, respiratory rate, skin temperature and blood
oxygen.

The range is the **10th–90th percentile of your own trailing baseline**. Three
of the five channels are raw sensor counts with no real-world unit, so an
absolute reference interval does not exist for them; the only comparison valid
across all five is against yourself. A value inside the range is unremarkable
*for you*; outside it is worth noticing. That is a statement about your own
variation, not a clinical reference interval — and it needs at least seven days
of history per channel before it says anything, which the card admits while it
is still filling.

The view shows each channel's current value, its band, the percentage
difference from your own median, and the live heart-rate sparkline for the day.

## Stress monitor — `#/stress`

A 0.0–3.0 arc gauge, a band breakdown (low / medium / high) with minutes in
each, and the day's curve.

**This is not WHOOP's Stress Score**, which is a proprietary model. It combines
the two things that reliably move together under autonomic arousal — heart rate
above your resting level (weighted 0.6) and short-term HRV below your own
baseline (0.4) — then expresses the result as a **position within your own
historical distribution**.

The scaling step is the important one. There is no absolute scale for "stress"
from a wrist sensor, so a raw number would be arbitrary. Scoring against your
own trailing distribution makes 0 and 3 mean something concrete: the calm and
busy ends of *your* range. It also means the scale is meaningless until roughly
ten hours of history exist to build that distribution from, so the view says so
rather than showing a number it cannot justify.

Values are smoothed over 5 minutes, and the heart-rate term is clamped at both
ends, so a spike past your configured maximum cannot push the raw signal above
1.0.

## Fitness age — `#/age`

Estimated VO₂ max, the age at which the population median VO₂ max equals yours,
and whether your own estimate is moving.

VO₂ max is estimated by the **heart-rate ratio method** (Uth et al. 2004,
*Eur J Appl Physiol*): `15.3 × HRmax / HRrest`. Two limits, both shown in the
view:

1. It was validated in **well-trained men** (r = 0.92 against measured VO₂
   max). It is an estimate, not a lab test, and it is weakest for people unlike
   that validation group.
2. It depends on a true maximum heart rate. Unless you have measured yours, the
   configured value is the 220−age rule, which carries roughly ±10–12 bpm of
   individual scatter. That error passes straight through to the number.

Ratios outside 2.0–5.5 are refused rather than reported, because they almost
always mean a bad resting-HR estimate rather than an extraordinary athlete.

"Fitness age" is then the age whose population median VO₂ max matches yours,
interpolated between ACSM/Cooper Institute reference bands and clamped to
20–80. Set `USER_AGE` and `USER_SEX` for the comparison; without an age it
still shows VO₂ max and the matching age, just not a delta.

### What this deliberately is not

* **Not biological or epigenetic age.** That needs methylation data. Nothing
  derived from heart rate can speak to it.
* **Not a "pace of ageing" multiplier.** The published pace-of-ageing measures
  come from longitudinal biomarker panels, not wearables. What *can* honestly
  be shown is whether your own fitness age is trending up or down over your own
  history, which is what the trend line does — a within-person slope, labelled
  as such.

**HRV is not scored against population norms anywhere in this app.** The
published norms (e.g. Nunan et al. 2010) are five-minute resting supine ECG;
this measures overnight wrist optical, which reads systematically differently.
Comparing them would look rigorous and be wrong, so HRV is only ever shown as
your own trend.
