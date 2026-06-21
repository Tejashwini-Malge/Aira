# Aira API Endpoints - Complete Audit

## Public Endpoints (No Authentication Required)

### Static Files
- `GET /` → Serves `index.html`
- `GET /<filename>` → Serves HTML pages and assets (login.html, signup.html, etc.)

### Auth Endpoints
- `POST /signup` → Create new user account
  - Input: `{ name, email, password }`
  - Output: User object + session cookie
  - Status: 200 (success), 400 (validation), 409 (user exists)

- `POST /login` → Authenticate existing user
  - Input: `{ email, password }`
  - Output: User object + session cookie
  - Status: 200 (success), 401 (invalid credentials)

---

## Protected Endpoints (Authentication Required)

### User & Session
- `GET /me` → Get current user info
  - Auth: Required
  - Output: `{ user: { id, name, email } }`

- `POST /logout` → Clear session
  - Auth: Required
  - Output: `{ success: true }`

### Persona Onboarding
- `GET /session/get-questions` → Fetch 10 onboarding questions
  - Auth: Required
  - Output: `{ questions: [...] }` (1 per dimension + 2 reflections)

- `POST /session/save-answers` → Store onboarding responses
  - Auth: Required
  - Input: `{ responses: [{ id, dimension, type, text, ...answer }] }`
  - Output: `{ success: true }`

- `POST /session/generate-persona` → Generate persona from answers (one-time)
  - Auth: Required
  - Output: `{ persona: { title, summary, dimensions } }`

- `GET /me/persona` → Get stored persona
  - Auth: Required
  - Output: `{ persona: { title, summary, dimensions } }`

### Speaking Practice
- `POST /me/speaking` → Record speaking session metrics
  - Auth: Required
  - Input: `{ mode, fluency, clarity, confidence, summary }`
  - Output: Session record

- `GET /me/speaking` → Fetch all speaking sessions
  - Auth: Required
  - Output: `{ sessions: [...] }`

### Reports & Analytics
- `GET /me/report` → Get full user report (persona + quizzes + speaking)
  - Auth: Required
  - Output: Aggregated user data

### Quiz System
- `POST /quiz/generate` → Generate topic quiz (5 questions)
  - Auth: Required
  - Input: `{ topic }`
  - Output: Quiz questions from LLM

- `POST /quiz/evaluate` → Evaluate quiz answers
  - Auth: Required
  - Input: `{ topic, answers }`
  - Output: Score + feedback + study plan

- `GET /me/quizzes` → Fetch quiz history
  - Auth: Required
  - Output: `{ quizzes: [...] }`

---

## Authentication Implementation

### Session-Based Auth
- Flask `flask.session` stores `user_id` in signed, secure, HttpOnly cookie
- All protected endpoints use `@login_required` decorator
- Missing or invalid session cookie → 401 Unauthorized

### Cookie Configuration
```python
app.config["SESSION_COOKIE_HTTPONLY"] = True  # No JS access
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # Same-origin
```

### Frontend Credentials
All fetch requests to protected endpoints use `credentials: 'include'`:
```javascript
fetch(url, {
  method: 'POST/GET',
  credentials: 'include',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(data)
})
```

---

## Verification Results

✓ All 12 protected endpoints return 401 without authentication
✓ All public endpoints are accessible without authentication
✓ Session cookie flows correctly through signup → save-answers → generate-persona
✓ Frontend auth guards redirect unauthenticated users to login
