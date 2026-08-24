"""Who the candidate is right now, resolved deterministically — no LLM call.

Aira's audience is students and people looking for their first role. Two bands
cover it:

  * student  — still studying, no professional job experience
  * fresher  — graduated, looking for a first role

That is the whole ladder. An earlier version also carried an `experienced` band
and a transition axis (fresh / continuing / switching) for career-changers. Both
were removed on 2026-08-07: no user in the product was a career-changer, the
`experienced` band's only occupant was a BTech student misclassified by resume
inference, and the transition axis never drove any prompt content. Rules written
for segments with no users cannot be validated and only make the real path harder
to reason about.

The band decides two things about every generated question — WHERE the scenario
may be set (BAND_RULES) and HOW HARD it should bite there (STAKES_RULES). They are
separate because they fail differently: a student handed a budget-allocation
scenario cannot answer it at all, while a fresher handed a trivially obvious
choice answers it correctly and reveals nothing.

WHERE THE ANSWER COMES FROM — declaration first, inference second.

`resume_data["experience_level"]` is written by an LLM in resume_agent from one
line of prompt. Measured against real production rows it is not dependable: six
users who all entered "B-Tec 4th year" were split across two bands; one BTech
student whose note described them as a "data science professional" came back
"experienced"; one row stored the literal string "student|fresher", the schema's
own option list echoed back as an answer. So onboarding asks the candidate
directly and that answer wins. The inference survives only as a fallback for rows
written before the question existed.

EXTENDING THIS. Both registries are plain dicts keyed by a closed vocabulary, so
a new band is a data change. Adding one back is cheap — which is exactly why it
should wait until there are users behind it.
"""
from dataclasses import dataclass

DEFAULT_BAND = "student"

BANDS = ("student", "fresher")

# The onboarding answer -> band. Must stay in step with
# onboarding_schema.CHOICE_FIELDS["experience"]; a test asserts it.
_PROFILE_BY_DECLARATION = {
    "Still studying": "student",
    "Graduated, looking for my first role": "fresher",
}

# FALLBACK ONLY — for personas created before the onboarding question existed.
# resume_agent still emits four values. "intern" and "experienced" both collapse
# to `fresher`, the top of the remaining ladder: if the inference is right, a
# fresher-level question is the closest fit available; if it is wrong (the
# observed case), a fresher question is still answerable by a student. Dropping
# them to `student` would be the worse error in the one case the inference got
# right. Anything outside the four misses this lookup and lands on DEFAULT_BAND.
_BAND_BY_EXPERIENCE_LEVEL = {
    "student": "student",
    "fresher": "fresher",
    "intern": "fresher",
    "experienced": "fresher",
}

# What settings are plausible, and — more importantly — what to refuse. The
# "NEVER" lines do the work: the model's untethered default is corporate, so
# naming the specific props to avoid is what actually moves it.
BAND_RULES = {
    "student": (
        "  - WHO THEY ARE: a student with no professional job experience yet. Set every\n"
        "    scenario somewhere they have actually been — a college group project, a lab\n"
        "    or assignment, a hackathon, a club or fest committee, a personal side project,\n"
        "    an internship application, a study group, a college placement drive.\n"
        "    NEVER give them authority they have never held. Do not write scenarios with a\n"
        "    product owner, a manager, stakeholders, a client account, a budget to allocate,\n"
        "    a team reporting to them, a performance review, or a sprint/quarter they own.\n"
        "    A question they cannot picture themselves in is a wasted question."
    ),
    "fresher": (
        "  - WHO THEY ARE: just graduated and looking for their first role — an internship\n"
        "    at most, no full job behind them. Scenarios should sit at the level of someone\n"
        "    doing the work, not running it: their own task or ticket, a code review, asking\n"
        "    a senior for help, a standup, onboarding onto an unfamiliar codebase, a final-year\n"
        "    project they still draw on, an interview or assessment they are preparing for.\n"
        "    NEVER give them ownership they would not have yet: no direct reports, no budget\n"
        "    authority, no hiring, no setting team-wide direction."
    ),
}

# How hard a question should BITE, as distinct from where it is set. BAND_RULES
# bound the world; these bound the difficulty inside it — a question whose "right"
# option is obvious measures nothing, because the candidate picks it from memory.
STAKES_RULES = {
    "student": (
        "  - HOW HARD IT SHOULD BITE: the situation itself must be easy to picture — all\n"
        "    the difficulty belongs in the trade-off, none of it in decoding the scenario.\n"
        "    They should not need workplace experience to understand what is being asked."
    ),
    "fresher": (
        "  - HOW HARD IT SHOULD BITE: they have prepared for interviews and can recite the\n"
        "    textbook answer to an obvious behavioural question, so an obviously-correct\n"
        "    option tells you nothing. Put two reasonable choices in real tension — speed\n"
        "    against getting it right, asking for help against working it out alone, what\n"
        "    they were told against what they can see — where each option gives something\n"
        "    up. Keep the consequences bounded to what someone at this stage would own."
    ),
}

# The onboarding `goal` dropdown's exact values (onboarding_schema.CHOICE_FIELDS).
# Each maps to what the question set should lean on, so the stated goal steers the
# scenario domain instead of sitting inert in the context block.
GOAL_RULES = {
    "Improving communication": (
        "  - WHAT THEY CAME FOR: they told us they want to improve how they COMMUNICATE.\n"
        "    Bias the scenarios toward moments where the outcome turns on explaining,\n"
        "    listening, persuading, or handling being misunderstood — explaining their work\n"
        "    to someone outside it, disagreeing without conflict, giving or taking feedback,\n"
        "    speaking up when unsure. Measure the same dimensions as always, but reach them\n"
        "    through communication-shaped situations."
    ),
    "Building confidence": (
        "  - WHAT THEY CAME FOR: they told us they want to build CONFIDENCE. Favour\n"
        "    scenarios where they get to show initiative, recover from a setback, or act\n"
        "    despite uncertainty. Do not write scenarios engineered to make them look bad;\n"
        "    the situation should have a real path where they come out well."
    ),
    "Placement / job preparation": (
        "  - WHAT THEY CAME FOR: they are preparing for PLACEMENTS. Lean toward situations\n"
        "    a campus interviewer would actually probe — their own projects, how they work\n"
        "    in a team, how they handle not knowing something."
    ),
    "Interview or exam coming up": (
        "  - WHAT THEY CAME FOR: they have an INTERVIEW OR EXAM coming up soon. Keep the\n"
        "    scenarios close to what they will face — preparation under time pressure,\n"
        "    being questioned on their own work, thinking on their feet."
    ),
    "Just exploring": (
        "  - WHAT THEY CAME FOR: they are still EXPLORING and have not committed to a goal.\n"
        "    Keep scenarios broad and low-pressure, and make them easy to engage with —\n"
        "    this person has the least invested and is the quickest to leave."
    ),
}


@dataclass(frozen=True)
class CandidateProfile:
    """The resolved framing for one candidate.

    Frozen so a resolved profile can be passed to any generator without a caller
    mutating another caller's view of the same user. `source` records whether the
    band came from the user's own answer or from the legacy resume inference —
    kept so a future backfill can find the rows still running on a guess.
    """

    band: str = DEFAULT_BAND
    goal: str = ""
    source: str = "inferred"      # "declared" | "inferred"

    @property
    def band_rule(self):
        return BAND_RULES.get(self.band, BAND_RULES[DEFAULT_BAND])

    @property
    def stakes_rule(self):
        return STAKES_RULES.get(self.band, STAKES_RULES[DEFAULT_BAND])

    @property
    def goal_rule(self):
        return GOAL_RULES.get(self.goal, "")

    def rules(self):
        """The framing rules to inject into a generation prompt, most binding
        first: where the scenario may be set, then how hard it must bite there,
        then what it should be about. A stakes rule that outranked the ceiling
        would talk a student back into workplace scenarios. Never empty — band
        always resolves, even if only to the default.
        """
        return "\n".join(r for r in (self.band_rule, self.stakes_rule, self.goal_rule) if r)


def resolve_profile(onboarding=None, resume_data=None):
    """Declaration beats inference.

    The user's own answer to the onboarding stage question is authoritative. Only
    when it is absent (a persona created before that question shipped) or
    unrecognised — including an answer from a retired option — does the resume's
    LLM-inferred experience_level get consulted.
    """
    onboarding = onboarding or {}
    resume_data = resume_data or {}

    goal = str(onboarding.get("goal") or "").strip()

    declared = str(onboarding.get("experience") or "").strip()
    if declared in _PROFILE_BY_DECLARATION:
        return CandidateProfile(band=_PROFILE_BY_DECLARATION[declared],
                                goal=goal, source="declared")

    raw_level = str(resume_data.get("experience_level") or "").strip().lower()
    band = _BAND_BY_EXPERIENCE_LEVEL.get(raw_level, DEFAULT_BAND)
    return CandidateProfile(band=band, goal=goal, source="inferred")
