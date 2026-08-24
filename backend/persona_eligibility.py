"""Shared refresh-eligibility rule for a locked Core Persona.

Lives in its own module (not session_controller.py) so persona_bp.py can use
it too without a circular import — session_controller.py already imports
_build_report_payload from persona_bp.py, so the reverse import isn't possible.
"""

# A Core Persona is generated once and then locked. It only becomes eligible for a
# deliberate refresh once this many practice sessions (quiz + speaking, combined)
# have happened since it was last built — one session is noisy (bad wifi, a nervous
# day, an unfamiliar topic); a handful gives Aira an actual pattern to react to.
# This number is an internal MVP threshold — never surface it to the user as a
# countdown; expose only the boolean "is there enough new evidence yet" verdict.
SESSIONS_REQUIRED_TO_REFRESH = 3


def total_sessions(user):
    return len(user.quiz_results) + len(user.speaking_sessions)


def refresh_eligible(total, persona):
    # A resume uploaded after the persona was built short-circuits the session
    # threshold — see Persona.resume_refresh_pending. getattr keeps this working
    # against a row object from before the column existed (and in tests that
    # stub a Persona with SimpleNamespace).
    if getattr(persona, "resume_refresh_pending", False):
        return True
    return total - persona.session_count_at_generation >= SESSIONS_REQUIRED_TO_REFRESH
